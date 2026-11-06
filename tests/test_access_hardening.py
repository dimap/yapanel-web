"""Регрессии доступа к панели: повреждённый файл, атомарная запись, блокировки и rate-limit."""
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

import access


PASSWORD = "correct-password"
IP = "203.0.113.7"


def manager(path):
    return access.PanelAccess(path)


@pytest.fixture
def fast_pbkdf2(monkeypatch):
    # Тесты не проверяют стойкость хэша — только логику доступа, поэтому не тратим время на 240k итераций.
    monkeypatch.setattr(access, "PBKDF2_ITER", 1000)


# ---------- повреждённый файл ----------
def test_missing_file_keeps_password_not_set_semantics(tmp_path):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)

    assert mgr.corrupt is False
    assert mgr.load_error is None
    assert mgr.enabled is False
    assert mgr.session_ok(None) is True
    with pytest.raises(access.AccessError, match="Пароль не задан"):
        mgr.login("127.0.0.1", "anything", "unittest")
    assert "error" not in mgr.info(None)


@pytest.mark.parametrize("payload", [
    b"{broken json",
    b"",
    b"[]",
    b"null",
    b'"string"',
    b'{"hash":"x","salt":"00"}',                  # хэш не hex
    b'{"hash":"00","salt":"zz"}',                 # соль не hex
    b'{"hash":"00","salt":"00","iter":0}',        # неверное число итераций
    b'{"hash":"00","salt":"00","sessions":[]}',   # сессии не объект
    b"\xff\xfe\x00",                              # файл не в UTF-8
])
def test_corrupt_file_is_fail_closed(tmp_path, payload):
    path = tmp_path / ".panel_auth.json"
    path.write_bytes(payload)
    mgr = manager(path)

    assert mgr.corrupt is True
    assert mgr.load_error
    assert mgr.enabled is True                    # защита считается включённой
    assert mgr.session_ok("some-token") is False

    info = mgr.info("some-token")
    assert info["enabled"] is True
    assert info["authed"] is False
    assert info["corrupt"] is True
    assert "поврежд" in info["error"]

    with pytest.raises(access.AccessError, match="поврежд"):
        mgr.login("127.0.0.1", PASSWORD, "unittest")
    with pytest.raises(access.AccessError, match="поврежд"):
        mgr.set_password("", PASSWORD, "unittest")
    with pytest.raises(access.AccessError, match="поврежд"):
        mgr.disable("")
    assert mgr.logout_others("some-token") == 0
    assert path.read_bytes() == payload            # повреждённый файл не перезаписан


def test_corrupt_file_can_be_recovered_by_deleting_it(tmp_path, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    path.write_text("{broken", encoding="utf-8")
    broken = manager(path)
    assert broken.corrupt is True

    path.unlink()
    recovered = manager(path)
    assert recovered.corrupt is False
    token = recovered.set_password("", PASSWORD, "unittest")
    assert recovered.session_ok(token)


# ---------- атомарная запись ----------
def test_save_uses_unique_temp_file_in_same_dir_and_fsync(tmp_path, monkeypatch, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    mgr.data = {"hash": "00" * 32, "salt": "00" * 16, "iter": 1, "sessions": {}}

    replacements, fsynced = [], []
    real_replace, real_fsync = os.replace, os.fsync

    def spy_replace(src, dst):
        replacements.append((Path(src), Path(dst)))
        real_replace(src, dst)

    def spy_fsync(fd):
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(access.os, "replace", spy_replace)
    monkeypatch.setattr(access.os, "fsync", spy_fsync)
    mgr._save()

    assert len(replacements) == 1
    temp, destination = replacements[0]
    assert temp.parent == path.parent
    assert destination == path
    assert not temp.exists()
    assert fsynced, "перед replace содержимое должно попасть на диск"
    assert [p.name for p in path.parent.iterdir()] == [path.name]
    assert json.loads(path.read_text(encoding="utf-8")) == mgr.data


def test_save_does_not_damage_existing_file_on_failure(tmp_path, monkeypatch, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    mgr.set_password("", PASSWORD, "unittest")
    before = path.read_bytes()

    def broken_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(access.os, "replace", broken_replace)
    mgr.data["hash"] = "11" * 32
    with pytest.raises(OSError, match="replace failed"):
        mgr._save()

    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_save_cleans_temp_file_when_serialization_fails(tmp_path, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    mgr.set_password("", PASSWORD, "unittest")
    before = path.read_bytes()

    mgr.data["not_serializable"] = object()
    with pytest.raises(TypeError):
        mgr._save()

    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]


@pytest.mark.skipif(os.name == "nt", reason="Windows не хранит POSIX-права")
def test_saved_file_has_private_permissions(tmp_path, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    mgr.set_password("", PASSWORD, "unittest")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------- пауза не держит lock ----------
def test_login_throttle_does_not_block_other_requests(tmp_path, monkeypatch, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    token = mgr.set_password("", PASSWORD, "unittest")

    errors = []

    def failed_login():
        try:
            mgr.login("198.51.100.1", "wrong-password", "attacker")
        except access.AccessError as error:
            errors.append(error)

    started = time.monotonic()
    worker = threading.Thread(target=failed_login, daemon=True)
    worker.start()

    deadline = started + 2
    while "198.51.100.1" not in mgr.fails and time.monotonic() < deadline:
        time.sleep(0.005)
    assert "198.51.100.1" in mgr.fails, "неудачная попытка должна зарегистрироваться"

    begin = time.monotonic()
    assert mgr.session_ok(token) is True
    session_wait = time.monotonic() - begin

    begin = time.monotonic()
    other_token = mgr.login("198.51.100.2", PASSWORD, "other")
    login_wait = time.monotonic() - begin

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors and "Неверный пароль" in str(errors[0])
    assert mgr.session_ok(other_token) is True
    assert session_wait < 0.3, f"session_ok ждал паузу login: {session_wait:.3f} с"
    assert login_wait < 0.35, f"другой login ждал паузу: {login_wait:.3f} с"
    assert time.monotonic() - started >= 0.6, "пауза после неверного пароля должна остаться"


@pytest.mark.parametrize("method,args", [
    ("set_password", ("wrong-password", "new-password", "unittest")),
    ("disable", ("wrong-password",)),
])
def test_password_operations_throttle_outside_lock(tmp_path, monkeypatch, fast_pbkdf2, method, args):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    token = mgr.set_password("", PASSWORD, "unittest")

    errors = []

    def failed_operation():
        try:
            getattr(mgr, method)(*args)
        except access.AccessError as error:
            errors.append(error)

    worker = threading.Thread(target=failed_operation, daemon=True)
    worker.start()
    time.sleep(0.25)                    # поток успевает войти в паузу после проверки пароля

    begin = time.monotonic()
    assert mgr.session_ok(token) is True
    waited = time.monotonic() - begin

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors and "Текущий пароль неверный" in str(errors[0])
    assert waited < 0.3, f"session_ok ждал паузу {method}: {waited:.3f} с"


# ---------- logout / set_password / disable ----------
def test_logout_revokes_only_its_session(tmp_path, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    first = mgr.set_password("", PASSWORD, "unittest")
    second = mgr.login(IP, PASSWORD, "unittest")

    mgr.logout(first)
    assert mgr.session_ok(first) is False
    assert mgr.session_ok(second) is True
    mgr.logout(None)
    mgr.logout("unknown-token")
    assert mgr.session_ok(second) is True


def test_logout_others_keeps_current_session(tmp_path, fast_pbkdf2):
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    current = mgr.set_password("", PASSWORD, "unittest")
    other = mgr.login(IP, PASSWORD, "unittest")

    assert mgr.logout_others(current) == 1
    assert mgr.session_ok(current) is True
    assert mgr.session_ok(other) is False


def test_set_password_rotates_sessions_and_password(tmp_path, monkeypatch, fast_pbkdf2):
    monkeypatch.setattr(access, "_throttle_delay", lambda: 0)
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    old_token = mgr.set_password("", PASSWORD, "unittest")

    new_token = mgr.set_password(PASSWORD, "new-password", "unittest")
    assert mgr.session_ok(old_token) is False
    assert mgr.session_ok(new_token) is True
    assert mgr._check_password("new-password") is True
    assert mgr._check_password(PASSWORD) is False

    with pytest.raises(access.AccessError, match="Текущий пароль неверный"):
        mgr.set_password("wrong-password", "third-password", "unittest")
    assert mgr._check_password("new-password") is True

    login_token = mgr.login(IP, "new-password", "unittest")
    assert mgr.session_ok(login_token) is True


def test_disable_removes_password_after_current_check(tmp_path, monkeypatch, fast_pbkdf2):
    monkeypatch.setattr(access, "_throttle_delay", lambda: 0)
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    token = mgr.set_password("", PASSWORD, "unittest")

    with pytest.raises(access.AccessError, match="Текущий пароль неверный"):
        mgr.disable("wrong-password")
    assert mgr.enabled is True
    assert mgr.session_ok(token) is True

    mgr.disable(PASSWORD)
    assert mgr.enabled is False
    assert mgr.data == {}
    assert mgr.session_ok("anything") is True
    assert json.loads(path.read_text(encoding="utf-8")) == {}


# ---------- rate-limit и очистка fails ----------
def test_rate_limit_still_blocks_after_five_failures(tmp_path, monkeypatch, fast_pbkdf2):
    monkeypatch.setattr(access, "_throttle_delay", lambda: 0)
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    mgr.set_password("", PASSWORD, "unittest")

    for attempt in range(5):
        with pytest.raises(access.AccessError, match="Неверный пароль"):
            mgr.login(IP, f"wrong-{attempt}", "attacker")
    assert mgr.fails[IP][0] == 5

    with pytest.raises(access.AccessError, match="Слишком много попыток"):
        mgr.login(IP, PASSWORD, "attacker")
    assert mgr.fails[IP][0] == 5          # во время блокировки счётчик не растёт

    token = mgr.login("198.51.100.9", PASSWORD, "unittest")
    assert mgr.session_ok(token) is True


def test_old_and_excess_failure_records_are_pruned(tmp_path, monkeypatch, fast_pbkdf2):
    monkeypatch.setattr(access, "_throttle_delay", lambda: 0)
    monkeypatch.setattr(access, "FAILS_MAX", 3)
    path = tmp_path / ".panel_auth.json"
    mgr = manager(path)
    mgr.set_password("", PASSWORD, "unittest")

    now = time.time()
    mgr.fails = {
        "old": (5, now - access.FAILS_TTL - 1),
        "b1": (1, now - 3),
        "b2": (1, now - 2),
        "b3": (1, now - 1),
    }
    with pytest.raises(access.AccessError, match="Неверный пароль"):
        mgr.login("new-ip", "wrong-password", "attacker")

    assert "old" not in mgr.fails
    assert len(mgr.fails) <= access.FAILS_MAX
    assert "new-ip" in mgr.fails
