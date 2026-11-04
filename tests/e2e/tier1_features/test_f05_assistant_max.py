"""Tier 1 Feature Test: F05 - Station Max Custom Voice Assistant.

Verifies assistant_max engine:
1. Normalization and trigger matching.
2. Status serialization and error handling.
3. Glagol client commands and cancellation.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

import assistant_max


def test_f05_trigger_matching():
    triggers = ["спроси gpt", "спроси модель", "нейросеть"]
    q, matched = assistant_max.match_trigger("Спроси GPT, сколько звезд в галактике?", triggers)
    assert matched is True
    assert q == "сколько звезд в галактике?"

    q, matched = assistant_max.match_trigger("Какая сегодня погода?", triggers)
    assert matched is False


def test_f05_speech_cleanup():
    raw = "**Тест** _форматирования_ #разметки `кода`"
    cleaned = assistant_max.for_speech(raw)
    assert "*" not in cleaned
    assert "_" not in cleaned
    assert "#" not in cleaned
    assert "`" not in cleaned


def test_f05_daemon_status_and_lifecycle(tmp_path):
    daemon = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    daemon.status_path = tmp_path / "webpanel_assistant.json"
    daemon.write_status()
    assert daemon.status_path.is_file()

    st = json.loads(daemon.status_path.read_text(encoding="utf-8"))
    assert st["version"] == assistant_max.VERSION
    assert st["state"] == "idle"
    assert st["enabled"] is True


def test_f05_daemon_stop(tmp_path):
    daemon = assistant_max.AssistantDaemon(work_dir=str(tmp_path))
    daemon.status_path = tmp_path / "webpanel_assistant.json"
    daemon.state = "thinking"
    with patch.object(daemon.glagol, "stop", return_value=True):
        daemon.stop_current()
    assert daemon.state == "idle"
