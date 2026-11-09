"""Station Max hosts ownership and optional adbguard v2 integration.

No installation or mount operations belong to this module. An existing guard owns
OTA protection; the panel only changes its analytics flag. Legacy installs retain
panel-managed hosts, but only when the panel can prove mount ownership.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import threading
import time

OTA = (
    "quasar.s3.yandex.net", "updater.mobile.yandex.net",
    "ota.yandex.net", "ota.yandex.ru",
)
ANALYTICS = (
    "startup.mobile.yandex.net", "startup.appmetrica.yandex.net",
    "report.appmetrica.yandex.net", "clck.yandex.net",
    "startup.appmetrica.yango.com", "report.appmetrica.yango.com",
    "clck.yasmina.yango.com", "appmetrica.yandex.net", "metrica.yandex.net",
)
FLAG = "/data/local/tmp/adbguard-analytics.disabled"
TARGET = "/system/etc/hosts"
LOCK = threading.RLock()


def backend_setting():
    name = os.getenv("YAPANEL_MAX_PRIVACY_BACKEND", "auto").strip().lower()
    return name if name in ("auto", "panel", "adbguard-v2") else "auto"


def command(adb, script, timeout=15):
    """Use plain ADB, bypassing an optional privileged file/command agent."""
    factory = getattr(adb, "only_adb", None)
    context = factory() if callable(factory) else None
    # Тестовые и некоторые облегчённые ADB-адаптеры могут не иметь
    # настоящего контекстного менеджера. Не требуем его без необходимости.
    if not (hasattr(context, "__enter__") and hasattr(context, "__exit__")):
        context = contextlib.nullcontext()
    with context:
        response = adb.shell(script, check=False, timeout=timeout)
    if not isinstance(response, (tuple, list)) or len(response) < 3:
        raise RuntimeError("ADB shell вернул некорректный ответ")
    out, err, rc = response[:3]
    if rc:
        raise RuntimeError((str(err or out or "ADB shell failed")).strip()[-300:])
    return str(out or "").replace("\r", "")


def probe(adb, panel_hosts):
    # One snapshot avoids stitching together mount/hosts observations from different calls.
    script = (
        "printf 'PRV_GUARD='; getprop init.svc.adbguard; "
        "printf 'PRV_SCRIPT='; [ -f /odm/bin/adbguard.sh ] && echo yes || echo no; "
        "printf 'PRV_PROFILES='; "
        "[ -f /odm/etc/hosts-ota ] && [ -f /odm/etc/hosts-full ] && echo yes || echo no; "
        f"printf 'PRV_FLAG='; [ -f {FLAG} ] && echo off || echo on; "
        f"awk '$5==\"{TARGET}\" {{print \"PRV_MOUNT:\"$4}}' /proc/self/mountinfo; "
        f"printf 'PRV_OWN='; [ -f {shlex.quote(panel_hosts)} ] && "
        f"[ \"$(stat -c '%d:%i' {shlex.quote(panel_hosts)} 2>/dev/null)\" = "
        f"\"$(stat -c '%d:%i' {TARGET} 2>/dev/null)\" ] && echo yes || echo no; "
        "printf 'PRV_HOSTS_BEGIN\\n'; cat /system/etc/hosts; "
        "printf '\\nPRV_HOSTS_END\\n'"
    )
    raw = command(adb, script)
    def field(key):
        m = re.search(r"(?m)^PRV_" + key + r"=([^\n]*)$", raw)
        return m.group(1).strip() if m else ""
    h = re.search(r"(?s)PRV_HOSTS_BEGIN\n(.*?)\nPRV_HOSTS_END", raw)
    rules = {}
    if h:
        for row in h.group(1).splitlines():
            cols = row.split("#", 1)[0].split()
            if len(cols) > 1:
                for host in cols[1:]:
                    rules.setdefault(host.lower(), set()).add(cols[0])
    return {
        "guard": field("GUARD"), "guard_script": field("SCRIPT") == "yes",
        "profiles": field("PROFILES") == "yes",
        "desired": field("FLAG"), "roots": re.findall(r"(?m)^PRV_MOUNT:([^\n]+)$", raw),
        "panel_owns_top": field("OWN") == "yes", "rules": rules,
        "hosts_read": h is not None,
    }


def classify(snap, forced="auto"):
    """Return external mode, panel mode or fail-closed conflict without mutation."""
    if forced == "adbguard-v2" or snap["profiles"] or snap["guard_script"] or snap["guard"] == "running":
        return "adbguard-v2"
    if not snap["roots"]:
        return "panel"
    if len(snap["roots"]) == 1 and snap["panel_owns_top"]:
        return "panel"
    return "conflict"


BLOCK_ADDRESSES = {"0.0.0.0", "127.0.0.1", "::1"}


def hosts_blocked(snap, hosts):
    """True, когда активный hosts уже перенаправляет все указанные домены в localhost."""
    if not snap.get("hosts_read"):
        return False
    rules = snap.get("rules") or {}
    return all(rules.get(host.lower(), set()) & BLOCK_ADDRESSES for host in hosts)


def external_status(snap):
    rules = snap["rules"]
    ota_ok = snap["hosts_read"] and all(
        {"127.0.0.1", "::1"}.issubset(rules.get(host, set())) for host in OTA
    )
    analytics_ok = snap["hosts_read"] and all(
        {"0.0.0.0", "::1"}.issubset(rules.get(host, set())) for host in ANALYTICS
    )
    analytics_absent = snap["hosts_read"] and all(
        not (rules.get(host, set()) & {"0.0.0.0", "127.0.0.1", "::1"})
        for host in ANALYTICS
    )
    roots = snap["roots"]
    if roots == ["/etc/hosts-ota", "/etc/hosts-full"] and ota_ok and analytics_ok:
        mode = "on"
    elif roots == ["/etc/hosts-ota"] and ota_ok and analytics_absent:
        mode = "off"
    else:
        mode = "unknown"
    managed = mode != "unknown" and snap["profiles"]
    applied = managed and snap["guard"] == "running" and snap["desired"] == mode
    if not snap["profiles"]:
        error = "Не найдены профили adbguard v2. Проверьте установку менеджера hosts"
    elif not ota_ok:
        error = "Не подтверждена блокировка OTA; переключение запрещено"
    elif not managed:
        error = "Неизвестный mount/hosts. Операции с системным hosts запрещены"
    elif snap["guard"] != "running":
        error = "Служба adbguard остановлена"
    elif not applied:
        error = "Состояние adbguard ещё не совпадает с флагом"
    else:
        error = ""
    return {"backend": "adbguard-v2", "enabled": mode == "on", "mode": mode,
            "desired": snap["desired"], "managed": managed, "applied": applied,
            "ota_protected": ota_ok, "hosts": list(ANALYTICS),
            "blocked": mode == "on" and analytics_ok, "queued": 0, "error": error}


def update_old_hook(adb, hook, custom):
    """Remove only the obsolete panel-owned bind; preserve other hook commands.

    Make a one-time backup before editing. This is idempotent and intentionally
    never mounts/unmounts files or deletes a pre-existing panel hosts file.
    """
    line = "bind " + custom + " " + TARGET
    qhook, qline = shlex.quote(hook), shlex.quote(line)
    backup = shlex.quote(hook + ".pre-adbguard-v2.bak")
    temporary = shlex.quote(hook + ".privacy-migrate.tmp")
    escaped_line = re.escape(line).replace("/", "\\/")
    script = (
        f"[ -f {qhook} ] || exit 0; "
        f"grep -Fxq {qline} {qhook} || exit 0; "
        f"[ -f {backup} ] || cp -p {qhook} {backup} || exit 2; "
        f"cp -p {qhook} {temporary} || exit 3; "
        # Restore the current hook, not the oldest saved backup, on migration failure.
        f"sed -i '/^{escaped_line}$/d' {qhook} && sh -n {qhook} && "
        f"! grep -Fxq {qline} {qhook}; result=$?; "
        f"if [ $result -ne 0 ]; then cp -p {temporary} {qhook}; "
        f"rm -f {temporary}; exit $result; fi; "
        f"rm -f {temporary}"
    )
    command(adb, script)


def install(server, panel_cls):
    """Install Station Max-specific privacy adapter; keep legacy mode available."""
    if getattr(panel_cls, "_max_privacy_wrapped", False):
        return
    old_status = panel_cls.privacy_status
    old_set = panel_cls.privacy_set
    old_purge = panel_cls.privacy_purge

    def privacy_status(self, migrate=True):
        with LOCK:
            try:
                snapshot = probe(self.adb, server.HOSTS_CUSTOM)
            except (OSError, RuntimeError) as exc:
                return {"backend": "unknown", "enabled": False, "managed": False,
                        "applied": False, "hosts": list(ANALYTICS), "queued": 0,
                        "error": "Не удалось проверить hosts: " + str(exc)}
            kind = classify(snapshot, backend_setting())
            if kind == "adbguard-v2":
                status = external_status(snapshot)
                if migrate and status["applied"]:
                    try:
                        update_old_hook(self.adb, server.BOOT_HOOK, server.HOSTS_CUSTOM)
                    except (OSError, RuntimeError) as exc:
                        status.update(managed=False, applied=False,
                                      error="Не удалось обновить старый boot_hook.sh: " + str(exc))
                return status
            if kind == "conflict":
                blocked = hosts_blocked(snapshot, server.ANALYTICS_HOSTS)
                error = ("Аналитика уже блокируется внешним hosts (/odm). "
                         "Панель не будет его менять; OTA-защита остаётся на внешнем менеджере."
                         if blocked else
                         "Обнаружен чужой hosts mount. Панель не будет снимать его")
                if migrate:
                    try:
                        # Убираем только старую строку панели из boot_hook.sh,
                        # чтобы она не перекрыла внешний hosts при загрузке.
                        update_old_hook(self.adb, server.BOOT_HOOK, server.HOSTS_CUSTOM)
                    except (OSError, RuntimeError) as exc:
                        error += " Не удалось убрать старый bind из boot_hook.sh: " + str(exc)
                return {"backend": "conflict", "enabled": False, "managed": False,
                        "applied": False, "blocked": blocked,
                        "hosts": list(server.ANALYTICS_HOSTS), "queued": 0,
                        "error": error}
            try:
                result = old_status(self)
            except (OSError, RuntimeError) as exc:
                return {"backend": "panel", "enabled": False, "managed": False,
                        "applied": False, "hosts": list(server.ANALYTICS_HOSTS), "queued": 0,
                        "error": "Не удалось проверить hosts панели: " + str(exc)}
            result.update(backend="panel", managed=True, applied=True)
            return result

    def privacy_set(self, enabled):
        with LOCK:
            state = self.privacy_status()
            if not state.get("managed") or not state.get("applied"):
                raise server.AdbError(state.get("error") or "Не удалось определить владельца hosts")
            if state["backend"] == "panel":
                # A fresh ownership check immediately before old code can umount.
                latest = probe(self.adb, server.HOSTS_CUSTOM)
                if classify(latest, backend_setting()) != "panel":
                    raise server.AdbError("Изменился владелец hosts. Повторите после проверки")
                return old_set(self, enabled)
            if state["enabled"] == bool(enabled):
                return state
            script = f"rm -f {FLAG}" if enabled else f"touch {FLAG}"
            try:
                command(self.adb, script)
                for _ in range(30):
                    now = self.privacy_status()
                    if now.get("applied") and now["enabled"] == bool(enabled):
                        return now
                    if not now.get("ota_protected"):
                        break
                    time.sleep(0.5)
            except (OSError, RuntimeError) as exc:
                failure = str(exc)
            else:
                failure = "Требуемое состояние не подтверждено"
            rollback = f"rm -f {FLAG}" if state["enabled"] else f"touch {FLAG}"
            try:
                command(self.adb, rollback)
                for _ in range(30):
                    previous = self.privacy_status()
                    if previous.get("applied") and previous["enabled"] == state["enabled"]:
                        raise server.AdbError(f"{failure}; прежнее состояние восстановлено")
                    time.sleep(0.5)
            except server.AdbError:
                raise
            except (OSError, RuntimeError):
                pass
            raise server.AdbError(f"{failure}; откат не подтверждён, проверьте adbguard")

    def privacy_purge(self):
        state = self.privacy_status()
        if state.get("backend") == "adbguard-v2":
            raise server.AdbError("Для adbguard очистка AppMetrica не поддерживается")
        if not state.get("managed") or not state.get("applied"):
            raise server.AdbError(state.get("error") or "Владелец hosts не подтверждён")
        return old_purge(self)

    panel_cls.privacy_status = privacy_status
    panel_cls.privacy_set = privacy_set
    panel_cls.privacy_purge = privacy_purge
    panel_cls._max_privacy_wrapped = True
