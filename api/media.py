"""HTTP-маршруты медиа: музыка, радио, визуализатор и Bluetooth."""
import glagol

from .router import Response, Router


def _radio_logo(req):
    logo = req.panel.radio_logo(req.q("name", ""))
    if not logo:
        return Response({"error": "логотип не найден"}, status=404)
    return Response(logo[0], content_type=logo[1],
                    headers=(("Content-Security-Policy", "default-src 'none'"),))


def _music_lyrics(req):
    if req.q("id"):
        return req.panel.music_lyrics(req.q("id"))
    return req.panel.ymusic.lyrics_by_name(
        req.q("title", ""), req.q("artist", ""), req.q("album"), float(req.q("duration") or 0))


def register(router: Router):
    router.get("/api/music", lambda r: r.panel.music())
    router.get("/api/music/info", lambda r: r.panel.music_info(r.q("id", "")))
    router.get("/api/music/lyrics", _music_lyrics)
    router.get("/api/music/queue", lambda r: r.panel.music_queue())
    router.get("/api/music/similar", lambda r: (r.panel._need_login(), r.panel.ymusic.similar(r.q("id", "")))[1])
    router.get("/api/music/artist", lambda r: (r.panel._need_login(), r.panel.ymusic.artist(r.q("id", "")))[1])
    router.get("/api/auth", lambda r: {**r.panel.auth.info(), "oauth_url": glagol.OAUTH_URL})
    router.get("/api/auth/qr", lambda r: r.panel.auth_qr_status())
    router.get("/api/radio", lambda r: r.panel.radio_status())
    router.get("/api/radio/search", lambda r: r.panel.radio_search(r.q("q", ""), r.q("country", "")))
    router.get("/api/radio/logo", _radio_logo)
    router.get("/api/viz", lambda r: r.panel.viz_status())
    router.get("/api/bt", lambda r: r.panel.bt_state())
    router.get("/api/bt/memes", lambda r: r.panel.bt_memes_status())
    router.get("/api/btout/eq", lambda r: r.panel.btout_eq_status())

    router.post("/api/music/cmd", lambda r: r.panel.music_cmd(r.data))
    router.post("/api/music/like", lambda r: r.panel.music_like(r.data["id"], bool(r.data.get("like", True))))
    router.post("/api/music/dislike", lambda r: r.panel.music_dislike(r.data["id"]))
    router.post("/api/music/play", lambda r: r.panel.music_play(r.data["id"], r.data.get("type", "track")))
    router.post("/api/auth/qr", lambda r: r.panel.auth_qr_start())
    router.post("/api/auth/oauth", lambda r: r.panel.auth_oauth(r.data["url"]))
    router.post("/api/auth/logout", lambda r: r.panel.auth_logout())
    router.post("/api/radio", lambda r: r.panel.radio_set(r.data))
    router.post("/api/radio/play", lambda r: r.panel.radio_play(r.data))
    router.post("/api/radio/stop", lambda r: r.panel.radio_stop())
    router.post("/api/radio/miss-seen", lambda r: r.panel.assistant_command({"radio_miss_seen": True}))
    router.post("/api/radio/check", lambda r: r.panel.radio_check(r.data.get("url")))
    router.post("/api/radio/alice", lambda r: r.panel.radio_alice(r.data.get("name")))
    router.post("/api/viz", lambda r: r.panel.viz_set(bool(r.data.get("on"))))
    router.post("/api/bt", lambda r: r.panel.bt_action(r.data))
    router.post("/api/bt/memes", lambda r: r.panel.bt_memes_set(bool(r.data.get("enabled"))))
    router.post("/api/bt/memes/reset", lambda r: r.panel.bt_memes_reset(r.data.get("kind")))
    router.post("/api/btout/scan", lambda r: r.panel.btout_scan(r.data.get("seconds", 12)))
    router.post("/api/btout/use", lambda r: r.panel.btout_use(r.data.get("mac")))
    router.post("/api/btout/off", lambda r: r.panel.btout_off())
    router.post("/api/btout/eq", lambda r: r.panel.btout_eq(r.data.get("preset"), r.data.get("bands")))
