"""Доменные HTTP-маршруты панели, собранные тонким роутером."""
from .router import ApiRequest, Response, Router


def build_router():
    from . import dsp, fs, media, party, yam
    router = Router()
    for module in (party, dsp, yam, media, fs):
        module.register(router)
    return router


ROUTER = build_router()
