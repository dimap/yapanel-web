"""Station Max Bluetooth sound daemon integration (panel-managed)."""
import importlib.util
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

import profiles

ROOT = Path(__file__).resolve().parents[1]


def fresh_server():
    spec = importlib.util.spec_from_file_location("isolated_panel_bt", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAdb:
    is_local = True

    def push(self, local, remote, timeout=None):
        remote = Path(remote)
        remote.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, remote)

    def shell(self, command, check=False, timeout=None):
        return "", "", 0


def make_panel(tmp_path):
    s = fresh_server()
    profiles.install(s, "max")
    root = tmp_path / "webpanel"
    root.mkdir()
    s.WEB_DIR = str(root)
    s.BT_MEMES_DIR = str(root / "bt-memes")
    s.BT_MEMES_BIN = f"{s.BT_MEMES_DIR}/bt-memes"
    s.BT_MEMES_SOUNDS = f"{s.BT_MEMES_DIR}/sounds"
    s.BT_MEMES_BACKUP = f"{s.BT_MEMES_DIR}/backup"
    s.BT_MEMES_LOG = f"{s.BT_MEMES_DIR}/bt-memes.log"
    s.BT_MEMES_SD = str(tmp_path / "bt-memes.pid")
    s.BT_MEMES_AUTOSTART = str(root / "bt_memes.autostart")
    s.BT_MEMES_TARGET_ON = str(tmp_path / "quasar" / "common" / "bluetooth_turning_on_jingle.opus")
    s.BT_MEMES_TARGET_OFF = str(tmp_path / "quasar" / "common" / "bluetooth_turning_off_jingle.opus")
    s.BT_MEMES_TARGET_SAY_ON = str(tmp_path / "quasar" / "ru-RU" / "bluetooth_turning_on.mp3")
    s.BT_MEMES_TARGET_SAY_OFF = str(tmp_path / "quasar" / "ru-RU" / "bluetooth_turning_off.mp3")
    for target in (s.BT_MEMES_TARGET_ON, s.BT_MEMES_TARGET_OFF,
                   s.BT_MEMES_TARGET_SAY_ON, s.BT_MEMES_TARGET_SAY_OFF):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"stock-" + Path(target).name.encode())
    p = s.Panel(FakeAdb())
    return s, p


def test_status_install_and_upload_apply_and_restore(tmp_path):
    s, p = make_panel(tmp_path)
    st = p.bt_memes_status()
    assert st["supported"] is True
    assert st["installed"] is False

    st = p.bt_memes_install()
    assert st["installed"] is True
    assert Path(s.BT_MEMES_BIN).is_file()
    assert Path(s.BT_MEMES_SOUNDS, "silent.mp3").is_file()
    assert Path(s.BT_MEMES_BACKUP, "bluetooth_turning_on_jingle.opus").read_bytes().startswith(b"stock-")
    # silent.mp3 is applied to both phrase files on install
    assert Path(s.BT_MEMES_TARGET_SAY_ON).read_bytes() == Path(s.BT_MEMES_SOUNDS, "silent.mp3").read_bytes()

    p.bt_memes_upload("on", b"OggS" + b"new-on" * 10)
    assert Path(s.BT_MEMES_SOUNDS, "ruko_blud.opus").read_bytes().startswith(b"OggS")
    assert Path(s.BT_MEMES_TARGET_ON).read_bytes().startswith(b"OggS")

    with pytest.raises(Exception):
        p.bt_memes_upload("off", b"not-an-ogg")

    p.bt_memes_reset("on")
    assert not Path(s.BT_MEMES_SOUNDS, "ruko_blud.opus").exists()
    assert Path(s.BT_MEMES_TARGET_ON).read_bytes().startswith(b"stock-")


def test_boot_hook_and_hook_users_include_bt_memes():
    s = fresh_server()
    profiles.install(s, "max")
    source = (ROOT / "max_profile.py").read_text(encoding="utf-8")
    assert "BT_MEMES_AUTOSTART" in source
    assert "pidof bt-memes" in source
    assert "bt-memes.log" in source
    users = s.Panel(Mock())._hook_users_script()
    assert "bt_memes.autostart" in users


def test_distribution_assets_exist():
    binary = ROOT / "device" / "bt_memes" / "bt-memes"
    silent = ROOT / "device" / "bt_memes" / "sounds" / "silent.mp3"
    assert binary.is_file() and binary.stat().st_size > 1000
    assert silent.is_file()
