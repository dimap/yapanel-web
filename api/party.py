"""HTTP-маршруты светомузыки (Party Mode)."""
from .router import Router


def register(router: Router):
    router.get("/api/party/status", lambda r: r.panel.party_status())
    router.get("/api/party/bulbs", lambda r: r.panel.party_bulbs_get())
    router.get("/api/party/bulbs/scan", lambda r: r.panel.party_bulbs_scan(float(r.q("timeout", 3.0))))
    router.get("/api/party/cinema/status", lambda r: r.panel.party_cinema_status())
    router.get("/api/party/cinema/sync_tv", lambda r: r.panel.party_cinema_sync_tv())

    router.post("/api/party/settings", lambda r: r.panel.party_settings(r.data))
    router.post("/api/party/restore_white", lambda r: r.panel.party_restore_white())
    router.post("/api/party/reconnect_all", lambda r: r.panel.party_reconnect())
    router.post("/api/party/service", lambda r: r.panel.party_service(r.data.get("action")))
    router.post("/api/party/mode", lambda r: r.panel.party_mode(r.data.get("mode"), r.data.get("params")))
    router.post("/api/party/cinema/control", lambda r: r.panel.party_cinema_control(r.data.get("action"), r.data.get("value")))
    router.post("/api/party/cinema/spell", lambda r: r.panel.party_cinema_spell(r.data.get("spell"), r.data.get("direction")))
    router.post("/api/party/cinema/sync_tv", lambda r: r.panel.party_cinema_sync_tv())
    router.post("/api/party/bulbs/position", lambda r: r.panel.party_bulb_position(
        r.data.get("id"), r.data.get("pos_x"), r.data.get("pos_y", 0.0), r.data.get("name")))
    router.post("/api/party/bulbs/test", lambda r: r.panel.party_bulb_test(
        r.data.get("id"), r.data.get("action", "solo"), r.data.get("color", "white")))
    router.post("/api/party/bulbs/add", lambda r: r.panel.party_bulb_add(r.data))
    router.post("/api/party/bulbs/update", lambda r: r.panel.party_bulb_update(r.data))
    router.post("/api/party/bulbs/delete", lambda r: r.panel.party_bulb_delete(r.data.get("id")))
    router.post("/api/party/bulbs/import", lambda r: r.panel.party_bulbs_import(r.data))
    router.post("/api/party/ambilight/frame", lambda r: r.panel.party_ambilight_frame(r.data))
