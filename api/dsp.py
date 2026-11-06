"""HTTP-маршруты Hi-Fi DSP."""
from .router import Router


def register(router: Router):
    router.get("/api/dsp", lambda r: r.panel.dsp_status())
    router.post("/api/dsp", lambda r: r.panel.dsp_action(r.data))
