"""Защитные тесты assistant_max: схема config.json, безопасные URL, ключи и custom TTS."""
import json
import os
import stat
import sys
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assistant_max


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, limit=None):
        return self.body if limit is None else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NO_CONFIG = object()


def make_daemon(tmp_path: Path, cfg=_NO_CONFIG):
    if cfg is not _NO_CONFIG:
        (tmp_path / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return assistant_max.AssistantDaemon(work_dir=str(tmp_path))


# ---------- схема config.json ----------

def test_valid_config_is_loaded(tmp_path):
    key = "sk-valid-1234567890"
    d = make_daemon(tmp_path, {
        "enabled": False,
        "triggers": ["  включи помощника  ", "спроси gpt"],
        "llm": {
            "base_url": "https://api.example.com/v1/",
            "model": "my-model",
            "system_prompt": "Будь краток.",
            "max_tokens": 256,
            "history": 3,
            "proxy": "",
        },
        "tts": {
            "provider": "openai",
            "base_url": "https://tts.example.com/v1/",
            "model": "tts-1-hd",
            "voice": "nova",
            "speed": 1.25,
        },
        "led": {"enabled": False, "color": "#00ff00"},
        "mode": {"enter": ["включи ассистента"], "exit": [], "timeout_min": 42, "follow_up": False},
        "keys": {"https://API.example.com/v1/": key},
    })
    assert d.cfg_error == ""
    assert d.cfg["enabled"] is False
    assert d.cfg["triggers"] == ["включи помощника", "спроси gpt"]
    assert d.cfg["llm"]["base_url"] == "https://api.example.com/v1"
    assert d.cfg["llm"]["model"] == "my-model"
    assert d.cfg["llm"]["max_tokens"] == 256
    assert d.cfg["llm"]["history"] == 3
    assert d.cfg["llm"]["api_key"] == key      # ключ взят по нормализованному адресу
    assert d.cfg["keys"] == {"https://api.example.com/v1": key}
    assert d.cfg["tts"]["provider"] == "openai"
    assert d.cfg["tts"]["base_url"] == "https://tts.example.com/v1"
    assert d.cfg["tts"]["voice"] == "nova"
    assert d.cfg["tts"]["speed"] == 1.25
    assert d.cfg["led"] == {"enabled": False, "color": "#00ff00"}
    assert d.cfg["mode"]["timeout_min"] == 42


@pytest.mark.parametrize("bad", [
    [],
    "not a config",
    42,
    None,
])
def test_non_object_root_uses_defaults(tmp_path, bad):
    d = make_daemon(tmp_path, bad)
    assert d.cfg_error
    assert d.cfg["llm"]["model"] == assistant_max.DEFAULT_CONFIG["llm"]["model"]
    assert d.cfg["tts"]["provider"] == "alice"


def test_broken_sections_do_not_crash(tmp_path):
    d = make_daemon(tmp_path, {
        "enabled": "yes",
        "triggers": "nope",
        "llm": "oops",
        "tts": [],
        "led": 7,
        "mode": "x",
        "keys": [],
    })
    assert d.cfg_error
    assert d.cfg["enabled"] is True
    assert d.cfg["llm"] == assistant_max.DEFAULT_CONFIG["llm"]
    assert d.cfg["tts"] == assistant_max.DEFAULT_CONFIG["tts"]
    assert d.cfg["led"] == assistant_max.DEFAULT_CONFIG["led"]
    assert d.cfg["mode"] == assistant_max.DEFAULT_CONFIG["mode"]
    assert d.cfg["keys"] == {}


def test_numeric_limits_are_clamped(tmp_path):
    d = make_daemon(tmp_path, {
        "llm": {"max_tokens": 10 ** 9, "history": -7},
        "tts": {"speed": 9},
        "mode": {"timeout_min": -5, "follow_up": "да"},
    })
    assert d.cfg_error
    assert d.cfg["llm"]["max_tokens"] == assistant_max.MAX_TOKENS_LIMITS[1]
    assert d.cfg["llm"]["history"] == 0
    assert d.cfg["tts"]["speed"] == assistant_max.TTS_SPEED_LIMITS[1]
    assert d.cfg["mode"]["timeout_min"] == assistant_max.TIMEOUT_MIN_LIMITS[0]
    assert d.cfg["mode"]["follow_up"] is True   # неверный тип -> дефолт


def test_wrong_field_types_use_defaults(tmp_path):
    d = make_daemon(tmp_path, {
        "llm": {"max_tokens": "много", "history": 2.5, "model": 7},
        "tts": {"speed": "быстро", "provider": 1, "voice": []},
        "mode": {"timeout_min": "десять", "enter": "фраза"},
        "led": {"color": "красный"},
    })
    assert d.cfg_error
    assert d.cfg["llm"]["max_tokens"] == assistant_max.DEFAULT_CONFIG["llm"]["max_tokens"]
    assert d.cfg["llm"]["history"] == assistant_max.DEFAULT_CONFIG["llm"]["history"]
    assert d.cfg["llm"]["model"] == assistant_max.DEFAULT_CONFIG["llm"]["model"]
    assert d.cfg["tts"]["speed"] == assistant_max.DEFAULT_CONFIG["tts"]["speed"]
    assert d.cfg["tts"]["provider"] == "alice"
    assert d.cfg["mode"]["timeout_min"] == assistant_max.DEFAULT_CONFIG["mode"]["timeout_min"]
    assert d.cfg["mode"]["enter"] == assistant_max.DEFAULT_CONFIG["mode"]["enter"]
    assert d.cfg["led"]["color"] == assistant_max.DEFAULT_CONFIG["led"]["color"]


def test_huge_numbers_do_not_crash_validation():
    cfg, errors = assistant_max._validate_config({
        "tts": {"speed": 10 ** 1000},
        "llm": {"max_tokens": 10 ** 1000, "history": 10 ** 1000},
        "mode": {"timeout_min": 10 ** 1000},
    })
    assert errors
    assert cfg["tts"]["speed"] == assistant_max.DEFAULT_CONFIG["tts"]["speed"]
    assert cfg["llm"]["max_tokens"] == assistant_max.MAX_TOKENS_LIMITS[1]
    assert cfg["llm"]["history"] == assistant_max.HISTORY_LIMITS[1]
    assert cfg["mode"]["timeout_min"] == assistant_max.TIMEOUT_MIN_LIMITS[1]


def test_broken_json_keeps_defaults_and_error(tmp_path):
    (tmp_path / "config.json").write_text('{"llm": {', encoding="utf-8")
    d = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    assert d.cfg_error.startswith("Ошибка чтения конфига")
    assert d.cfg == json.loads(json.dumps(assistant_max.DEFAULT_CONFIG))


def test_cfg_error_survives_reload(tmp_path):
    (tmp_path / "config.json").write_text("{битый", encoding="utf-8")
    d = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    assert d.cfg_error
    d.cfg_mtime = 0
    d.reload_config_if_changed()
    assert d.cfg_error, "reload не должен стирать причину ошибки конфига"


# ---------- безопасные URL ----------

@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "http://localhost:11434/v1",
    "http://127.0.0.1:8080/v1",
    "http://[::1]:11434",
    "http://0.0.0.0:11434/v1",
    "http://192.168.1.10:1234/v1",
    "https://api.example.com/v1",
])
def test_urls_allowed(url):
    assert assistant_max.is_url_allowed(url)
    assert assistant_max.validate_base_url(url).rstrip("/") == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/v1",
    "gopher://example.com/",
    "http://169.254.169.254/latest/meta-data",
    "http://100.100.100.200/latest/meta-data",
    "http://metadata.google.internal/computeMetadata/v1",
    "http://metadata/",
    "http://169.254.10.10/v1",
    "http://2852039166/",
    "http://[::ffff:169.254.169.254]/",
    "https:///v1",
    "api.openai.com/v1",
])
def test_urls_blocked(url):
    assert not assistant_max.is_url_allowed(url)
    with pytest.raises(ValueError):
        assistant_max.validate_base_url(url)


def test_llm_does_not_request_unsafe_url(tmp_path):
    d = make_daemon(tmp_path, {"llm": {"base_url": "file:///etc/passwd"}})
    assert d.cfg["llm"]["base_url"] == ""
    assert "llm.base_url" in d.cfg_error
    with patch.object(urllib.request.OpenerDirector, "open") as opened:
        with pytest.raises(ValueError):
            d.call_llm("привет")
        opened.assert_not_called()


def test_tts_does_not_request_metadata(tmp_path):
    d = make_daemon(tmp_path, {
        "tts": {"provider": "openai", "base_url": "http://169.254.169.254/v1"},
    })
    assert d.cfg["tts"]["base_url"] == ""
    with patch.object(urllib.request.OpenerDirector, "open") as opened:
        assert d.speak_custom("привет") is False
        opened.assert_not_called()


def test_models_reports_unsafe_url_without_request(tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"base_url": "http://100.100.100.200/latest/meta-data"},
    }), encoding="utf-8")
    with patch.object(urllib.request.OpenerDirector, "open") as opened:
        assistant_max.print_models(str(cfg_path), "-")
        opened.assert_not_called()
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_models_with_broken_config_does_not_hit_network(tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{битый json", encoding="utf-8")
    with patch.object(urllib.request.OpenerDirector, "open") as opened:
        assistant_max.print_models(str(cfg_path), "-")
        opened.assert_not_called()
    out = json.loads(capsys.readouterr().out)
    assert "Ошибка конфига" in out["error"]


def test_models_override_does_not_reuse_old_provider_key(tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"base_url": "https://old.example/v1", "api_key": "sk-old-provider-123456"},
    }), encoding="utf-8")
    over_path = tmp_path / "override.json"
    over_path.write_text(json.dumps({
        "base_url": "https://new.example/v1",
    }), encoding="utf-8")

    with patch.object(urllib.request.OpenerDirector, "open",
                      return_value=FakeResponse(b'{"data": [{"id": "model-1"}]}')) as opened:
        assistant_max.print_models(str(cfg_path), str(over_path))

    request = opened.call_args.args[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert "authorization" not in headers
    assert request.full_url == "https://new.example/v1/models"
    assert json.loads(capsys.readouterr().out) == {"models": ["model-1"]}


# ---------- ключи ----------

def test_keys_are_resolved_for_llm_and_tts(tmp_path):
    llm_key = "sk-llm-key-123456"
    tts_key = "sk-tts-key-123456"
    d = make_daemon(tmp_path, {
        "llm": {"base_url": "https://llm.example.com/v1/"},
        "tts": {"provider": "openai", "base_url": "https://tts.example.com/v1"},
        "keys": {
            "https://llm.example.com/v1/": llm_key,
            "https://tts.example.com/v1": tts_key,
        },
    })
    assert d.cfg["llm"]["api_key"] == llm_key
    assert d.cfg["tts"]["api_key"] == tts_key
    # ключи разных провайдеров не теряются
    assert len(d.cfg["keys"]) == 2


def test_legacy_api_keys_are_kept(tmp_path):
    d = make_daemon(tmp_path, {
        "llm": {"base_url": "https://a.example/v1", "api_key": "legacy-llm-123456"},
        "tts": {"base_url": "https://b.example/v1", "api_key": "legacy-tts-123456"},
    })
    assert d.cfg["llm"]["api_key"] == "legacy-llm-123456"
    assert d.cfg["tts"]["api_key"] == "legacy-tts-123456"


def test_status_does_not_leak_keys(tmp_path):
    llm_key = "sk-super-secret-123456"
    tts_key = "sk-other-secret-654321"
    d = make_daemon(tmp_path, {
        "llm": {"base_url": "https://a.example/v1", "api_key": llm_key},
        "tts": {"base_url": "https://b.example/v1", "api_key": tts_key},
        "keys": {"https://b.example/v1": tts_key},
    })
    d.status_path = tmp_path / "status.json"
    d.error = f"boom {llm_key}"
    d.cfg_error = f"bad {tts_key}"
    d.history.append({
        "at": 1, "source": "voice", "question": f"q {llm_key}",
        "answer": f"a {tts_key}", "error": f"e {llm_key}", "ms": 1,
        "extra": {"note": f"nested {tts_key}"},
    })
    d.write_status()
    text = d.status_path.read_text(encoding="utf-8")
    assert llm_key not in text
    assert tts_key not in text
    assert "***" in text


def test_permission_helper_requests_0600_on_posix(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    calls = []

    class FakeOS:
        name = "posix"

        @staticmethod
        def chmod(path, mode):
            calls.append((path, mode))

    monkeypatch.setattr(assistant_max, "os", FakeOS)
    assistant_max._restrict_config_permissions(cfg_path)
    assert calls == [(cfg_path, 0o600)]


@pytest.mark.skipif(os.name != "posix", reason="права POSIX проверяются только на POSIX")
def test_config_with_keys_is_private(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "llm": {"base_url": "https://api.example.com/v1"},
        "keys": {"https://api.example.com/v1": "sk-private-123456"},
    }), encoding="utf-8")
    cfg_path.chmod(0o644)
    assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600


# ---------- custom TTS ----------

def test_custom_tts_payload_and_url(tmp_path):
    d = make_daemon(tmp_path)
    d.tts_path = tmp_path / "speech.wav"
    d.cfg["tts"] = {
        "provider": "openai",
        "base_url": "http://localhost:11434/v1/",
        "api_key": "sk-tts-secret-123456",
        "model": "tts-1-hd",
        "voice": "nova",
        "speed": 1.5,
    }
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return FakeResponse(b"RIFF....WAVE")

    with patch("urllib.request.urlopen", new=fake_urlopen):
        with patch.object(d, "_play_audio", return_value=True) as played:
            assert d.speak_custom("Привет, мир") is True
    assert seen["url"] == "http://localhost:11434/v1/audio/speech"
    assert seen["timeout"] == assistant_max.TTS_TIMEOUT
    assert seen["body"] == {
        "model": "tts-1-hd",
        "input": "Привет, мир",
        "voice": "nova",
        "response_format": "wav",
        "speed": 1.5,
    }
    assert seen["headers"]["authorization"] == "Bearer sk-tts-secret-123456"
    assert d.tts_path.read_bytes() == b"RIFF....WAVE"
    played.assert_called_once_with(d.tts_path)


def test_custom_tts_error_falls_back_to_google(tmp_path):
    d = make_daemon(tmp_path, {
        "tts": {"provider": "openai", "base_url": "http://localhost:11434/v1"},
    })
    d.google_tts_path = tmp_path / "google.mp3"

    with patch.object(d, "speak_custom", return_value=False) as custom, \
         patch("urllib.request.urlopen", return_value=FakeResponse(b"mp3-bytes")) as google, \
         patch.object(d, "_play_audio", return_value=True):
        d.speak("Привет")

    custom.assert_called_once()

    google.assert_called_once()
    request = google.call_args.args[0]
    assert "translate.google.com/translate_tts" in request.full_url
    assert d.google_tts_path.read_bytes() == b"mp3-bytes"
    assert d.state == "idle"


def test_alice_provider_does_not_use_custom_tts(tmp_path):
    d = make_daemon(tmp_path, {
        "tts": {"provider": "alice", "base_url": "http://localhost:11434/v1"},
    })
    with patch.object(d, "speak_custom") as custom, \
         patch.object(d.glagol, "say", return_value=True) as say:
        d.speak("Ответ")
    custom.assert_not_called()
    say.assert_called_once_with("Ответ")
    assert d.state == "idle"
    with patch("urllib.request.urlopen") as opened:
        assert d.speak_custom("Ответ") is False
        opened.assert_not_called()
