"""Свои фразы: пресеты «Сказать на колонке» и приветствие при старте панели."""
from .router import Router


def register(router: Router):
    router.get("/api/phrases", lambda r: r.panel.phrases_list())
    router.post("/api/phrases", lambda r: r.panel.phrases_save(r.data))
    router.post("/api/phrases/say", lambda r: r.panel.phrases_say(r.data.get("id")))
    router.post("/api/phrases/greeting", lambda r: r.panel.greeting_save(r.data))
    router.post("/api/phrases/greeting/say", lambda r: r.panel.greeting_say())
