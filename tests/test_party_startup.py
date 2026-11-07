"""Свет не должен сам включаться при рестарте панели."""
from types import SimpleNamespace
from unittest.mock import Mock

import server


def _panel_with_saved_state(monkeypatch, running, master=True):
    manager = SimpleNamespace(
        settings={"running": running, "master_enabled": master},
        start=Mock(),
    )
    monkeypatch.setattr(server.Panel, "get_party", lambda self, start=False: manager)
    config = SimpleNamespace(party=SimpleNamespace(auto_start=True))
    panel = server.Panel(Mock(), config)
    return panel, manager


def test_panel_does_not_start_party_when_saved_state_is_off(monkeypatch):
    _, manager = _panel_with_saved_state(monkeypatch, running=False)
    manager.start.assert_not_called()


def test_panel_resumes_party_when_it_was_explicitly_running(monkeypatch):
    _, manager = _panel_with_saved_state(monkeypatch, running=True)
    manager.start.assert_called_once_with()


def test_panel_does_not_resume_party_when_master_is_disabled(monkeypatch):
    _, manager = _panel_with_saved_state(monkeypatch, running=True, master=False)
    manager.start.assert_not_called()
