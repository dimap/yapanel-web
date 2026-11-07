"""Сбер Звук для панели: токен веб-сессии, поиск, «Мой поток» и ссылки на mp3.

API повторяет открытую реализацию для станции (MIT):
https://github.com/derangedabbot/zvuk-yandex-station-player
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import zvuk_queries as Q

GQL_URL = "https://zvuk.com/api/v1/graphql"
TINY_URL = "https://zvuk.com/api/tiny"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class Error(Exception):
    """Понятная пользователю ошибка «Звука»."""


class AuthError(Error):
    pass


class Auth:
    """Токен веб-сессии zvuk.com. Хранится только на компьютере панели, права 600."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    @property
    def token(self):
        return (self.data.get("token") or "").strip()

    def info(self):
        return {"has_token": bool(self.token)}

    def save(self, token):
        token = (token or "").strip()
        if not token:
            raise AuthError("токен «Звука» пустой")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": token}, f)
        self.data = {"token": token}

    def clear(self):
        self.data = {}
        self.path.unlink(missing_ok=True)


def _num(v, default=0):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return default


def _cover(node):
    img = (node or {}).get("image") or {}
    src = img.get("src") or ""
    return src.replace("{size}", "400x400")


def _artists(node):
    return ", ".join(str(a.get("title") or "") for a in (node or {}).get("artists") or [] if a.get("title"))


def track_from(v):
    """Трек из ответа GraphQL. У трека обязательно есть длительность — по ней и отличаем."""
    if not isinstance(v, dict) or v.get("id") is None or not v.get("title") or v.get("duration") is None:
        return None
    rel = v.get("release") or {}
    cover = _cover(rel)
    if not cover:
        for a in v.get("artists") or []:
            cover = _cover(a)
            if cover:
                break
    date = str(rel.get("date") or "")
    artist_ids = [str(a.get("id")) for a in v.get("artists") or [] if a.get("id") is not None]
    return {
        "id": str(v["id"]),
        "title": str(v.get("title") or ""),
        "artists": _artists(v) or _artists(rel),
        "artist_ids": artist_ids,
        "duration": _num(v.get("duration")),
        "cover": cover,
        "album_id": "" if rel.get("id") is None else str(rel.get("id")),
        "album": str(rel.get("title") or ""),
        "year": date[:4],
        "liked": False,
    }


def album_from(v):
    if not isinstance(v, dict) or v.get("id") is None or not v.get("title"):
        return None
    date = str(v.get("date") or "")
    return {"id": str(v["id"]), "title": str(v.get("title") or ""), "artists": _artists(v),
            "kind": str(v.get("type") or "album").lower(), "cover": _cover(v), "year": date[:4]}


def artist_from(v):
    if not isinstance(v, dict) or v.get("id") is None or not v.get("title"):
        return None
    return {"id": str(v["id"]), "title": str(v.get("title") or ""), "cover": _cover(v)}


def playlist_from(v):
    if not isinstance(v, dict) or v.get("id") is None or not v.get("title"):
        return None
    return {"id": str(v["id"]), "title": str(v.get("title") or ""), "cover": _cover(v),
            "tracks": int(_num(v.get("duration")))}


def _collect_tracks(node, out):
    if isinstance(node, dict):
        t = track_from(node)
        if t:
            if all(x["id"] != t["id"] for x in out):
                out.append(t)
            return
        for v in node.values():
            _collect_tracks(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_tracks(v, out)


class Api:
    def __init__(self, token):
        if not token:
            raise AuthError("сначала сохраните токен «Звука»")
        self.token = token

    def _gql(self, query, op, variables):
        body = json.dumps({"query": query, "operationName": op, "variables": variables}).encode()
        req = urllib.request.Request(GQL_URL, data=body, headers={
            "X-Auth-Token": self.token, "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*", "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://zvuk.com/", "Origin": "https://zvuk.com", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError("Звук не принял токен — скопируйте его из свежей веб-сессии zvuk.com") from None
            raise Error(f"Звук: HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise Error(f"Звук: нет связи ({e.reason})") from None
        except ValueError:
            raise Error("Звук вернул не JSON") from None
        if data.get("errors"):
            msg = str(data["errors"])[:300]
            raise Error(f"Звук: ошибка API: {msg}")
        return data.get("data") or {}

    def search(self, query, limit=10):
        query = (query or "").strip()
        if not query:
            raise Error("пустой поисковый запрос")
        try:
            data = self._gql(Q.SEARCH, "search", {"query": query, "limit": int(limit)})
        except Error:
            data = self._gql(Q.QUICK, "quickSearch", {"query": query, "limit": int(limit)})
        s = data.get("search") or {}
        groups = {
            "tracks": (s.get("tracks") or {}).get("items") or [],
            "albums": (s.get("releases") or {}).get("items") or [],
            "artists": (s.get("artists") or {}).get("items") or [],
            "playlists": (s.get("playlists") or {}).get("items") or [],
        }
        if not any(groups.values()):
            groups["tracks"] = (data.get("quickSearch") or {}).get("content") or []
        tracks, albums, artists, playlists = [], [], [], []
        for item in groups["tracks"]:
            t = track_from(item)
            if t and all(x["id"] != t["id"] for x in tracks):
                tracks.append(t)
        for item in groups["albums"]:
            a = album_from(item)
            if a and all(x["id"] != a["id"] for x in albums):
                albums.append(a)
        for item in groups["artists"]:
            a = artist_from(item)
            if a and all(x["id"] != a["id"] for x in artists):
                artists.append(a)
        for item in groups["playlists"]:
            p = playlist_from(item)
            if p and all(x["id"] != p["id"] for x in playlists):
                playlists.append(p)
        if not tracks:
            _collect_tracks(data, tracks)
        if not (tracks or albums or artists or playlists):
            raise Error(f"по запросу «{query}» ничего не нашлось")
        return {"query": query, "tracks": tracks[:limit], "albums": albums[:limit],
                "artists": artists[:limit], "playlists": playlists[:limit]}

    def personal_wave(self, count=10, mood=None, popular=None, genre=None, language=None, vocal=None):
        options = {"mood": mood or "energy:0.5,fun:0.5"}
        if popular is not None:
            try:
                options["popular"] = max(0.0, min(1.0, float(popular)))
            except (TypeError, ValueError):
                pass
        if isinstance(genre, (list, tuple, set)):
            genres = [str(g).strip() for g in genre if str(g).strip()]
        else:
            one = str(genre or "").strip()
            genres = [one] if one else []
        if genres:
            # Как в официальном клиенте Звука: массив жанров с уровнем LVL1.
            options["genre"] = [{"name": g, "type": "LVL1"} for g in genres]
        language = str(language or "").strip()
        if language:
            options["language"] = language
        if vocal is not None:
            # 0 — только инструментал («без слов»), 1 — с вокалом; так это делает клиент Звука.
            options["vocal"] = 1 if vocal else 0
        data = self._gql(Q.WAVE, "getPersonalWave", {
            "waveSrc": "AMAZME", "first": int(count), "options": options,
        })
        items = data.get("personalWaveContent") or []
        tracks = [t for t in (track_from(v) for v in items) if t]
        if not tracks:
            raise Error("«Мой поток» вернул пустой список")
        return tracks

    def stream_url(self, track_id):
        data = self._gql(Q.STREAM, "getStream", {"ids": [str(track_id)]})
        for item in data.get("mediaContents") or []:
            stream = (item or {}).get("stream") or {}
            for key in ("high", "mid"):
                url = stream.get(key)
                if isinstance(url, str) and url.startswith("http"):
                    return url
        url = f"{TINY_URL}/track/stream?id={urllib.parse.quote(str(track_id))}&quality=high"
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.token, "Referer": "https://zvuk.com/",
                                                   "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                v = json.loads(r.read() or b"{}")
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
            raise Error(f"Звук: не удалось получить поток ({e})") from None
        stream = ((v.get("result") or {}).get("stream"))
        if not stream:
            raise Error("Звук: нет ссылки на поток этого трека")
        return stream

    def download(self, url, dest):
        req = urllib.request.Request(url, headers={"Referer": "https://zvuk.com/", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise Error(f"Звук: поток HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise Error(f"Звук: поток не скачался ({e.reason})") from None
        if len(data) < 4096:
            raise Error("Звук: поток подозрительно короткий")
        tmp = Path(str(dest) + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        return len(data)

    def release_tracks(self, album_id):
        data = self._gql(Q.RELEASES, "getReleases", {"ids": [str(album_id)]})
        releases = data.get("getReleases") or []
        release = releases[0] if releases else {}
        tracks = [t for t in (track_from(v) for v in release.get("tracks") or []) if t]
        if not tracks:
            raise Error("Звук: в альбоме нет треков")
        return {"id": str(album_id), "title": release.get("title") or "", "tracks": tracks}

    def artist(self, artist_id):
        data = self._gql(Q.ARTISTS, "getArtists", {
            "ids": [str(artist_id)], "withPopTracks": True, "withReleases": False,
            "tracksLimit": 30, "releasesLimit": 1})
        artists = data.get("getArtists") or []
        a = artists[0] if artists else {}
        if not a:
            raise Error("Звук: исполнитель не найден")
        cover = _cover(a)
        popular = [t for t in (track_from(v) for v in a.get("popularTracks") or []) if t]
        return {"id": str(a.get("id") or artist_id), "name": a.get("title") or "",
                "cover": cover, "popular": popular}

    def like(self, track_id, on):
        query, op = (Q.ADD_LIKE, "addItemToCollection") if on else (Q.REMOVE_LIKE, "removeItemFromCollection")
        data = self._gql(query, op, {"id": str(track_id), "type": "track"})
        coll = data.get("collection") or {}
        return bool(coll.get("addItem") or coll.get("add_item")) if on else \
            bool(coll.get("removeItem") or coll.get("remove_item"))

    def dislike(self, track_id, on=True):
        """У Звука нет отдельного дизлайка: негативный сигнал — скрыть трек из рекомендаций."""
        query, op = (Q.ADD_HIDDEN, "addItemToHidden") if on else (Q.REMOVE_HIDDEN, "removeItemFromHidden")
        data = self._gql(query, op, {"id": str(track_id), "type": "track"})
        hidden = data.get("hidden_collection") or {}
        return bool(hidden.get("addItem") or hidden.get("add_item")) if on else \
            bool(hidden.get("removeItem") or hidden.get("remove_item"))

    def liked_tracks(self, limit=100):
        """Избранное: лайкнутые треки коллекции. userTracks отдаёт только id — детали через getTracks."""
        data = self._gql(Q.USER_TRACKS, "userTracks", {"orderBy": "dateAdded", "orderDirection": "desc"})
        rows = ((data.get("collection") or {}).get("tracks") or [])
        ids = [str(r.get("id")) for r in rows if r.get("id") is not None][:int(limit)]
        if not ids:
            return []
        order = {str(r.get("id")): i for i, r in enumerate(rows)}
        data = self._gql(Q.TRACKS, "getTracks", {"ids": ids})
        items = data.get("getTracks") or data.get("get_tracks") or []
        tracks = [t for t in (track_from(v) for v in items) if t]
        tracks.sort(key=lambda t: order.get(str(t["id"]), len(ids)))
        return tracks
