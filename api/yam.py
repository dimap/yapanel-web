"""HTTP-маршруты yam (демон) и yam 2 (пакетный менеджер)."""
from .router import Router


def register(router: Router):
    router.get("/api/yam/status", lambda r: r.panel.yam_status())
    router.get("/api/yam/registry", lambda r: r.panel.yam_registry(r.q("refresh") == "1"))
    router.get("/api/yam/plugins", lambda r: r.panel.yam_plugins())
    router.get("/api/yam/logs", lambda r: r.panel.yam_logs(r.q("id", "")))
    router.get("/api/yam/settings", lambda r: r.panel.yam_settings())
    router.get("/api/yam/version", lambda r: r.panel.yam_version(r.q("refresh") == "1"))
    router.get("/api/yam2/status", lambda r: r.panel.yam2_status(r.q("refresh") == "1"))
    router.get("/api/yam2/packages", lambda r: r.panel.yam2_packages())
    router.get("/api/yam2/catalog", lambda r: r.panel.yam2_catalog(r.q("refresh") == "1"))
    router.get("/api/yam2/log", lambda r: r.panel.yam2_service_log(r.q("id", "")))

    router.post("/api/yam/start", lambda r: r.panel.yam_start())
    router.post("/api/yam/install-daemon", lambda r: r.panel.yam_install_daemon(
        **({"drop_backups": True} if r.data.get("drop_backups") else {})))
    router.post("/api/yam/autostart", lambda r: r.panel.yam_autostart(bool(r.data["enabled"])))
    router.post("/api/yam/install", lambda r: r.panel.yam_install(r.data["id"], r.data["version"]))
    router.post("/api/yam/action", lambda r: r.panel.yam_action(r.data["action"], r.data["id"]))
    router.post("/api/yam/settings", lambda r: r.panel.yam_settings(r.data))
    router.post("/api/yam2/install", lambda r: r.panel.yam2_install())
    router.post("/api/yam2/update", lambda r: r.panel.yam2_update())
    router.post("/api/yam2/uninstall", lambda r: r.panel.yam2_uninstall())
    router.post("/api/yam2/registry", lambda r: r.panel.yam2_registry_add())
    router.post("/api/yam2/doctor", lambda r: r.panel.yam2_doctor(bool(r.data.get("repair"))))
    router.post("/api/yam2/pkg", lambda r: r.panel.yam2_pkg(
        r.data.get("action"), r.data["name"], bool(r.data.get("yes"))))
    router.post("/api/yam2/service", lambda r: r.panel.yam2_service(
        r.data["id"], r.data["action"], bool(r.data.get("stop_legacy"))))
