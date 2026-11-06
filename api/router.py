"""Тонкий роутер доменных HTTP-маршрутов панели.

Домен объявляет только путь и обработчик. ``server.Handler`` по-прежнему
отвечает за доступ, CSRF, разбор тела, журнал и отправку ответа. Обработчик
получает :class:`ApiRequest` и возвращает JSON-совместимое значение либо
:class:`Response` — для нестандартного статуса, типа или заголовков.
"""
from dataclasses import dataclass, field

JSON = "application/json; charset=utf-8"


@dataclass
class ApiRequest:
    panel: object
    query: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def q(self, name, default=None):
        return self.query.get(name, default)

    def arg(self, name):
        """Обязательное поле тела: отсутствие — 400, как у прежних d["name"]."""
        return self.data[name]


@dataclass
class Response:
    body: object = None
    status: int = 200
    content_type: str = JSON
    headers: tuple = ()


class Router:
    def __init__(self):
        self._routes = {"GET": {}, "POST": {}}

    def get(self, path, fn):
        self._add("GET", path, fn)

    def post(self, path, fn):
        self._add("POST", path, fn)

    def _add(self, method, path, fn):
        if path in self._routes[method]:
            raise ValueError(f"дублирующийся маршрут {method} {path}")
        self._routes[method][path] = fn

    def match(self, method, path):
        return self._routes.get(method, {}).get(path)
