"""Провайдер «Сбер Звук» для панели.

Звук качает панель, а играет штатный плеер станции: ему отдаётся ссылка на локальную
раздачу (`audio_play` через Glagol). Так Алиса, громкость и пауза остаются штатными.
Компьютер с панелью должен быть доступен станции по локальной сети.
"""
import hmac
import json
import os
import posixpath
import re
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import zvuk


class _SourceHandler(BaseHTTPRequestHandler):
    """Отдаёт скачанный mp3 станции: HEAD/GET, точная длина и Range (продолжение с позиции)."""
    cache = None
    secret = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _err(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(self, head):
        u = urllib.parse.urlparse(self.path)
        token = (urllib.parse.parse_qs(u.query).get("t") or [""])[0]
        if not hmac.compare_digest(str(token), str(self.secret)):
            return self._err(403)
        name = posixpath.basename(urllib.parse.unquote(u.path))
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.mp3", name):
            return self._err(404)
        path = Path(self.cache) / name
        try:
            size = path.stat().st_size
        except OSError:
            return self._err(404)
        start, end = 0, max(0, size - 1)
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        partial = bool(m and size)
        if partial:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
                end = size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
        length = end - start + 1 if size else 0
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head or not length:
            return
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_HEAD(self):
        self._serve(True)

    def do_GET(self):
        self._serve(False)


class ZvukPlayer:
    def __init__(self, panel, auth, cache_dir):
        self.panel = panel
        self.auth = auth
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.secret = secrets.token_urlsafe(18)
        self.lock = threading.RLock()
        self.source_srv = None
        self.media_port = None
        self.queue = []
        self.index = -1
        self.track = None
        self.playing = False
        self.paused = False
        self.offset = 0.0
        self.started = 0.0
        self.gen = 0
        self.last_search = None
        self.flow_active = False
        self.mood = None
        self.popular = None
        self.genre = []
        self.language = "any"
        self.vocal = None
        self._prune_cache(startup=True)
        threading.Thread(target=self._watch, daemon=True, name="zvuk-advance").start()

    # ---------- API ----------
    def api(self):
        return zvuk.Api(self.auth.token)

    def set_token(self, token):
        raw = (token or "").strip()
        # Разрешаем вставить и сам токен, и весь JSON со страницы zvuk.com/api/tiny/profile:
        # так пользователю не надо искать поле "token" руками.
        value = raw
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict):
                value = (data.get("token") or (data.get("result") or {}).get("token") or "")
        if not value or value == raw:
            m = re.search(r'"token"\s*:\s*"([^"]+)"', raw)
            if m:
                value = m.group(1)
        token = str(value or "").strip().strip('"')
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
            raise zvuk.AuthError("не нашёл токен: вставьте строку из поля \"token\" или весь JSON "
                                 "со страницы zvuk.com/api/tiny/profile")
        api = zvuk.Api(token)
        api.personal_wave(1)          # токен проверяем сразу, чтобы не сохранять нерабочий
        self.auth.save(token)
        with self.lock:
            self.last_search = None
        return self.state()

    # ---------- позиция ----------
    def position(self):
        with self.lock:
            if not self.track:
                return 0.0
            duration = float(self.track.get("duration") or 0)
            pos = self.offset
            if self.playing and not self.paused:
                pos += max(0.0, time.monotonic() - self.started)
            return round(min(duration, pos) if duration else pos, 2)

    def _set_state(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    # ---------- раздача для станции ----------
    def _lan_ip(self):
        host = getattr(self.panel.glagol, "host", None)
        if not host:
            host = (getattr(self.panel.adb, "serial", "") or "").split(":")[0]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((host or "192.168.0.1", 9))
                return s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            return socket.gethostbyname(socket.gethostname())

    def _ensure_source(self):
        """Своя раздача на 0.0.0.0: станция должна достать файл, даже если панель слушает только localhost."""
        with self.lock:
            if self.source_srv:
                return
            handler = type("ZvukSource", (_SourceHandler,), {"cache": self.cache, "secret": self.secret})
            port = int(os.environ.get("ZVUK_SRC_PORT", "8767") or 0)
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
            except OSError:
                srv = ThreadingHTTPServer(("0.0.0.0", 0), handler)   # порт занят — берём свободный
            srv.daemon_threads = True
            self.source_srv = srv
            self.media_port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True, name="zvuk-source").start()

    def _media_url(self, track):
        self._ensure_source()
        self.gen += 1
        host = self._lan_ip()
        return (f"http://{host}:{self.media_port}/zvuk/stream/{track['id']}.mp3"
                f"?t={self.secret}&g={self.gen}")

    def _download(self, track):
        tid = str((track or {}).get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
            raise zvuk.Error("некорректный id трека")
        path = self.cache / f"{tid}.mp3"
        if path.exists() and path.stat().st_size >= 4096:
            return path
        url = self.api().stream_url(tid)
        self.api().download(url, path)
        return path

    def _audio_play(self, track, offset, paused):
        url = self._media_url(track)
        res = self.panel.glagol.audio_play(url, track.get("title") or "", track.get("artists") or "",
                                           int(round(offset * 1000)), paused)
        status = (res or {}).get("status")
        if status not in ("SUCCESS", "timeout", None):
            raise zvuk.Error(f"станция не приняла трек: {status}")
        return url

    def _play(self, track, offset=0.0, paused=False):
        self._download(track)
        self._audio_play(track, offset, paused)
        self._set_state(track=track, offset=float(offset), started=time.monotonic(),
                        playing=True, paused=bool(paused))

    def _prune_cache(self, startup=False):
        """В кеше — только текущая очередь; остальное удаляем сразу (на старте — вообще всё)."""
        with self.lock:
            keep = {str(t.get("id")) for t in self.queue if t.get("id")}
            if self.track and self.track.get("id"):
                keep.add(str(self.track["id"]))
        try:
            for path in self.cache.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".mp3.part"):
                    if startup:
                        path.unlink(missing_ok=True)
                elif path.name.endswith(".mp3") and path.name[:-4] not in keep:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _watch(self):
        """Поток не должен обрываться в конце трека: заранее включаем следующий."""
        while True:
            time.sleep(.5)
            try:
                with self.lock:
                    track = self.track
                    active = self.playing and not self.paused
                if not track or not active:
                    continue
                duration = float(track.get("duration") or 0)
                if duration <= 0 or self.position() < duration - .35:
                    continue
                if self.station_foreign():
                    continue
                try:
                    self.next(1)
                except zvuk.Error:
                    self._set_state(playing=False, paused=False, offset=duration)
            except Exception:  # noqa: BLE001 — фоновый сторож не должен ронять панель
                pass

    def _extend_flow(self):
        with self.lock:
            mood = self.mood
            popular = self.popular
            genre = self.genre
            language = self.language
            vocal = self.vocal
        tracks = self.api().personal_wave(10, mood, popular, genre, language, vocal)
        if not tracks:
            raise zvuk.Error("Звук не дал треков для продолжения")
        with self.lock:
            self.queue = list(tracks)
            self.index = 0
            self.flow_active = True
        self._play(tracks[0])
        self._prune_cache()
        return self.state()

    @staticmethod
    def _genres(v):
        if isinstance(v, (list, tuple, set)):
            return [str(g).strip() for g in v if str(g).strip()]
        one = str(v or "").strip()
        return [one] if one else []

    # ---------- действия ----------
    def flow(self, mood=None, popular=None, genre=None, language=None, vocal=None):
        genres = self._genres(genre)
        tracks = self.api().personal_wave(10, mood, popular, genres, language, vocal)
        if not tracks:
            raise zvuk.Error("Звук не дал треков")
        with self.lock:
            self.queue = list(tracks)
            self.index = 0
            self.flow_active = True
            self.mood = mood
            self.popular = popular
            self.genre = genres
            self.language = str(language or "any")
            self.vocal = None if vocal is None else (1 if vocal else 0)
        self._play(tracks[0])
        self._prune_cache()
        return self.state()

    def search(self, q):
        found = self.api().search(q, 10)
        with self.lock:
            self.last_search = found
        return found

    def play_track(self, track):
        if not isinstance(track, dict) or not track.get("id"):
            raise zvuk.Error("не выбран трек")
        with self.lock:
            tracks = (self.last_search or {}).get("tracks") or []
            if any(t.get("id") == track.get("id") for t in tracks):
                self.queue = list(tracks)
                self.index = next(i for i, t in enumerate(self.queue) if t.get("id") == track.get("id"))
            else:
                self.queue = [track]
                self.index = 0
            self.flow_active = False
        self._play(track)
        self._prune_cache()
        return self.state()

    def queue_add(self, track):
        if not isinstance(track, dict) or not track.get("id"):
            raise zvuk.Error("не выбран трек")
        with self.lock:
            self.queue.append(track)
        self._prune_cache()
        return self.state()

    def queue_clear(self):
        with self.lock:
            cur = self.track
            self.queue = [cur] if cur else []
            self.index = 0 if cur else -1
        self._prune_cache()
        return self.state()

    def jump(self, i):
        with self.lock:
            i = int(i)
            if not 0 <= i < len(self.queue):
                raise zvuk.Error("нет такого трека в очереди")
            track = self.queue[i]
            self.index = i
        self._play(track)
        return self.state()

    def remove(self, i):
        with self.lock:
            i = int(i)
            if not 0 <= i < len(self.queue):
                raise zvuk.Error("нет такого трека в очереди")
            self.queue.pop(i)
            if i < self.index:
                self.index -= 1
            elif i == self.index:
                self.playing = False
                self.paused = False
                self.offset = 0.0
                self.track = None
                self.index = -1
        self._prune_cache()
        return self.state()

    def pause(self):
        with self.lock:
            if not self.track or self.paused or not self.playing:
                return self.state()
            pos = self.position()
            track = self.track
        self._audio_play(track, pos, True)
        self._set_state(playing=False, paused=True, offset=pos)
        return self.state()

    def resume(self):
        with self.lock:
            if not self.track:
                return self.flow()
            if self.playing and not self.paused:
                return self.state()
            pos, track = self.offset, self.track
        self._audio_play(track, pos, False)
        self._set_state(playing=True, paused=False, started=time.monotonic())
        return self.state()

    def stop(self):
        try:
            self.panel.glagol.send({"command": "stop"})
        except Exception:  # noqa: BLE001 — остановка не должна падать из-за связи
            pass
        self._set_state(playing=False, paused=False, offset=0.0, track=None, flow_active=False)
        return self.state()

    def next(self, step=1):
        extend = False
        with self.lock:
            i = self.index + step
            if step > 0 and i >= len(self.queue) and self.flow_active:
                extend = True
            elif not 0 <= i < len(self.queue):
                raise zvuk.Error("в очереди больше нет треков")
            else:
                track = self.queue[i]
                self.index = i
        if extend:
            return self._extend_flow()
        self._play(track)
        return self.state()

    def prev(self):
        return self.next(-1)

    def seek(self, seconds):
        with self.lock:
            if not self.track:
                return self.state()
            track = self.track
            pos = max(0.0, min(float(seconds), float(track.get("duration") or 0) or float(seconds)))
            paused = self.paused
        self._audio_play(track, pos, paused)
        self._set_state(offset=pos, started=time.monotonic(), playing=not paused, paused=paused)
        return self.state()

    def station_foreign(self):
        """Колонка заиграла не наш поток (голос Алисы/Яндекс) — панели пора вернуться на Яндекс."""
        with self.lock:
            track, playing, started = self.track, self.playing, self.started
        if not track or not playing or time.monotonic() - started < 3:
            return False
        state = getattr(self.panel.glagol, "state", None) or {}
        p = state.get("playerState") or {}
        title = str(p.get("title") or "").strip()
        if not title:
            return False
        return title.casefold() != str(track.get("title") or "").strip().casefold()

    def acknowledge_foreign(self):
        """Мы больше не владеем звуком: колонку не трогаем, просто гасим своё состояние."""
        with self.lock:
            self.playing = False
            self.paused = False
            self.offset = 0.0

    def reset_state(self):
        """Перед переключением на «Звук»: старое состояние не должно изображать чужой трек."""
        with self.lock:
            self.track = None
            self.playing = False
            self.paused = False
            self.offset = 0.0
            self.index = -1
            self.flow_active = False
            self.mood = None
            self.popular = None
            self.genre = []
            self.language = "any"
            self.vocal = None

    def album_tracks(self):
        with self.lock:
            track = self.track
        if not track or not track.get("album_id"):
            raise zvuk.Error("у текущего трека нет альбома")
        return self.api().release_tracks(track["album_id"])

    def similar_tracks(self):
        with self.lock:
            track = self.track
        if not track:
            raise zvuk.Error("сначала включите трек")
        ids = track.get("artist_ids") or []
        if not ids:
            return {"artist": "", "tracks": []}
        artist = self.api().artist(ids[0])
        tracks = [t for t in artist.get("popular") or [] if str(t.get("id")) != str(track.get("id"))]
        return {"artist": artist.get("name") or "", "tracks": tracks}

    def artist_info(self, artist_id=None):
        with self.lock:
            track = self.track
        ids = (track or {}).get("artist_ids") or []
        aid = artist_id or (ids[0] if ids else None)
        if not aid:
            raise zvuk.Error("у текущего трека нет исполнителя")
        return self.api().artist(aid)

    def like(self, track_id, on):
        with self.lock:
            track = self.track
        tid = track_id or (track or {}).get("id")
        if not tid:
            raise zvuk.Error("сначала включите трек")
        ok = self.api().like(tid, on)
        with self.lock:
            if self.track and str(self.track.get("id")) == str(tid):
                self.track["liked"] = bool(on)
            for t in self.queue:
                if str(t.get("id")) == str(tid):
                    t["liked"] = bool(on)
        return {"id": str(tid), "liked": bool(on), "ok": ok}

    def dislike(self, track_id, on=True):
        with self.lock:
            track = self.track
        tid = track_id or (track or {}).get("id")
        if not tid:
            raise zvuk.Error("сначала включите трек")
        ok = self.api().dislike(tid, on)
        with self.lock:
            if self.track and str(self.track.get("id")) == str(tid):
                self.track["hidden"] = bool(on)
            for t in self.queue:
                if str(t.get("id")) == str(tid):
                    t["hidden"] = bool(on)
        return {"id": str(tid), "hidden": bool(on), "ok": ok}

    def set_settings(self, mood=None, popular=None, genre=None, language=None, vocal=None):
        """Сохранить настроение/популярность/жанр/язык/вокал для следующих очередей, не запуская поток."""
        with self.lock:
            if mood is not None:
                self.mood = str(mood)
            if popular is not None:
                try:
                    self.popular = max(0.0, min(1.0, float(popular)))
                except (TypeError, ValueError):
                    pass
            else:
                self.popular = None
            if genre is not None:
                self.genre = self._genres(genre)
            if language is not None:
                self.language = str(language or "any").strip()
            if vocal is not None:
                self.vocal = 1 if vocal else 0
        return self.state()

    def favorites(self):
        return {"tracks": self.api().liked_tracks()}

    def play_list(self, tracks, index=0):
        tracks = [t for t in tracks or [] if isinstance(t, dict) and t.get("id")]
        if not tracks:
            raise zvuk.Error("список треков пуст")
        with self.lock:
            self.queue = list(tracks)
            self.index = max(0, min(int(index or 0), len(self.queue) - 1))
            self.flow_active = False
        self._play(self.queue[self.index])
        self._prune_cache()
        return self.state()

    def action(self, d):
        action = str((d or {}).get("action") or "")
        if action == "flow":
            return self.flow(d.get("mood"), d.get("popular"), d.get("genre"), d.get("language"), d.get("vocal"))
        if action == "search":
            return self.search(str(d.get("q") or ""))
        if action in ("play", "play-id"):
            return self.play_track(d.get("track"))
        if action == "play-list":
            return self.play_list(d.get("tracks") or [], d.get("index") or 0)
        if action == "album":
            return self.album_tracks()
        if action == "similar":
            return self.similar_tracks()
        if action == "artist":
            return self.artist_info(d.get("id"))
        if action == "like":
            return self.like(d.get("track_id"), bool(d.get("on")))
        if action == "dislike":
            return self.dislike(d.get("track_id"), bool(d.get("on", True)))
        if action == "settings":
            return self.set_settings(d.get("mood"), d.get("popular"), d.get("genre"), d.get("language"), d.get("vocal"))
        if action == "favorites":
            return self.favorites()
        if action == "queue-add":
            return self.queue_add(d.get("track"))
        if action == "queue-clear":
            return self.queue_clear()
        if action == "jump":
            return self.jump(d.get("index"))
        if action == "remove":
            return self.remove(d.get("index"))
        if action == "pause":
            return self.pause()
        if action == "resume":
            return self.resume()
        if action == "stop":
            return self.stop()
        if action == "next":
            return self.next(1)
        if action == "prev":
            return self.prev()
        if action == "seek":
            return self.seek(d.get("position") or 0)
        raise zvuk.Error("неизвестное действие «Звука»")

    # ---------- для панели ----------
    def command(self, d):
        """Команды, которые приходят из плеера панели (play/pause/next/...)."""
        cmd = (d or {}).get("command")
        if cmd == "play":
            return self.resume()
        if cmd == "stop":
            return self.pause()
        if cmd == "next":
            return self.next(1)
        if cmd == "prev":
            return self.prev()
        if cmd == "rewind":
            return self.seek(d.get("position") or 0)
        if cmd == "sendText":
            text = str(d.get("text") or "").lower()
            if "волн" in text:
                return self.flow()
        # остальное (лайк, перемешать, повтор, громкость, Алиса) — не про Звук
        return {"status": "SUCCESS"}

    def state(self):
        with self.lock:
            track, queue, index = self.track, list(self.queue), self.index
            playing, paused = self.playing, self.paused
            mood, popular = self.mood, self.popular
            genre, language = self.genre, self.language
            vocal = self.vocal
        return {
            "provider": "zvuk",
            "has_token": bool(self.auth.token),
            "playing": bool(playing and not paused),
            "paused": bool(paused),
            "track": track,
            "queue": queue,
            "index": index,
            "position": self.position(),
            "duration": float(track.get("duration") or 0) if track else 0,
            "device": "Сбер Звук",
            "mood": mood,
            "popular": popular,
            "genre": genre,
            "language": language,
            "vocal": vocal,
        }

    def music_info(self):
        """Состояние в форме Glagol — существующий плеер панели показывает его без переделки."""
        with self.lock:
            track, queue, index = self.track, list(self.queue), self.index
            playing, paused = self.playing, self.paused
        gstate = getattr(self.panel.glagol, "state", None) or {}
        volume = gstate.get("volume")
        pos = self.position()
        p = None
        if track:
            p = {
                "id": track.get("id"),
                "title": track.get("title") or "—",
                "subtitle": track.get("artists") or "",
                "duration": float(track.get("duration") or 0),
                "progress": pos,
                "playerType": "zvuk",
                "type": "Track",
                "playlistType": "Track",
                "playlistDescription": "Сбер Звук",
                "extra": {"coverURI": track.get("cover") or "", "album": track.get("album") or ""},
                "entityInfo": {"repeatMode": "None", "shuffled": False},
                "hasNext": index + 1 < len(queue) or self.flow_active,
                "hasPrev": index > 0,
            }
        now = time.time()
        state = {"playing": bool(playing and not paused), "playerState": p}
        if isinstance(volume, (int, float)):
            state["volume"] = float(volume)
        return {
            "provider": "zvuk",
            "auth": {"logged_in": True, "login": "Сбер Звук", "method": "zvuk", "provider": "zvuk"},
            "glagol": {"connected": True, "error": None, "updated": now, "progress_at": now, "state": state},
        }
