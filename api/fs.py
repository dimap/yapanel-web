"""HTTP-маршруты файлового менеджера.

Потоковые загрузка и скачивание (``/api/fs/upload`` и ``/api/fs/download``)
остаются в ``Handler``: им нужны тело запроса и отдача файла по частям.
"""
from .router import Router


def register(router: Router):
    router.get("/api/fs/list", lambda r: r.panel.fs_list(r.q("path", "/")))
    router.get("/api/fs/read", lambda r: r.panel.fs_read(r.q("path", "")))

    router.post("/api/fs/write", lambda r: r.panel.fs_write(r.data["path"], r.data["text"]))
    router.post("/api/fs/delete", lambda r: r.panel.fs_delete(r.data["path"]))
    router.post("/api/fs/rename", lambda r: r.panel.fs_rename(r.data["path"], r.data["name"]))
    router.post("/api/fs/mkdir", lambda r: r.panel.fs_mkdir(r.data["path"]))
    router.post("/api/fs/play", lambda r: r.panel.fs_play(r.data["path"]))
