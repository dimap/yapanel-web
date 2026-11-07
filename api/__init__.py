"""Доменные HTTP-маршруты панели, собранные тонким роутером."""
from .router import ApiRequest, Response, Router


def build_router():
    from . import dsp, fs, media, party, phrases, yam
    router = Router()
    for module in (party, dsp, yam, media, fs, phrases):
        module.register(router)
    return router


ROUTER = build_router()
