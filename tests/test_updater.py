"""Проверка автообновления панели: GitHub Releases, кеш и обработка ошибок."""
from __future__ import annotations

import json
import urllib.error

import pytest

import server


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.payload


def release_payload(tag="v0.4.0", published="2026-11-06T00:00:00Z"):
    return {
        "tag_name": tag,
        "published_at": published,
        "html_url": f"https://github.com/example/releases/tag/{tag}",
    }


def test_newer_version_comparison():
    assert server.PanelUpdater._newer("0.4.0", "0.3.9")
    assert server.PanelUpdater._newer("1.0.0", "0.99.99")
    assert not server.PanelUpdater._newer("0.3.0", "0.3.0")
    assert not server.PanelUpdater._newer("0.3.0-rc1", "0.3.0")
    assert server.PanelUpdater._newer("0.3.0", "0.3.0-rc1")


def test_check_returns_latest_release_and_update_flag(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append((request.full_url, timeout))
        return FakeResponse(release_payload())

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    updater = server.PanelUpdater()
    result = updater.check(refresh=True)

    assert result["install"] in ("zip", "git", "docker", "android")
    assert result["latest"] == "0.4.0"
    assert result["latest_date"] == "2026-11-06"
    assert result["update"] is True
    assert result["url"].endswith("/v0.4.0")
    assert "disabled" not in result
    assert "error" not in result
    assert calls and calls[0][0].endswith("/releases/latest")
    assert calls[0][1] == 15


def test_check_uses_cache_until_refresh(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        return FakeResponse(release_payload())

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    updater = server.PanelUpdater()
    updater.check(refresh=True)
    updater.check()
    assert len(calls) == 1

    updater.check(refresh=True)
    assert len(calls) == 2


def test_check_reports_network_error(monkeypatch):
    def fake_urlopen(request, timeout=0):
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    updater = server.PanelUpdater()
    result = updater.check(refresh=True)

    assert "error" in result
    assert "недоступен" in result["error"].lower()


def test_check_reports_missing_releases(monkeypatch):
    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    updater = server.PanelUpdater()
    result = updater.check(refresh=True)

    assert "error" in result
    assert "релизов" in result["error"].lower()


def test_check_keeps_last_successful_release_on_refresh_error(monkeypatch):
    state = {"fail": False}

    def fake_urlopen(request, timeout=0):
        if state["fail"]:
            raise OSError("connection reset")
        return FakeResponse(release_payload())

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    updater = server.PanelUpdater()
    first = updater.check(refresh=True)
    assert first["latest"] == "0.4.0"

    state["fail"] = True
    second = updater.check(refresh=True)
    assert second["latest"] == "0.4.0"
    assert second["update"] is True
    assert "error" in second
