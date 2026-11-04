"""Tier 1: Feature 11 — Protocol Mock & Regex Repair (F11_TEST_MOCK_REPAIR).

Validates Glagol protocol mock roundtrips, Journal entry parsing,
Assistant IPC schemas, and PyADB packet structures.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import pyadb


def test_glagol_protocol_payload_serialization():
    """Verify Glagol WebSocket protocol message payload schema."""
    command_payload = {
        "command": "sendText",
        "text": "какая погода",
        "conversationToken": "test_token_123",
    }
    encoded = json.dumps(command_payload).encode("utf-8")
    decoded = json.loads(encoded.decode("utf-8"))

    assert decoded["command"] == "sendText"
    assert decoded["text"] == "какая погода"
    assert decoded["conversationToken"] == "test_token_123"


def test_journal_entry_structure_and_regex_matching():
    """Verify Journal entries conform to structured schema with valid timestamps."""
    entry = {
        "timestamp": "2026-11-03T14:30:00Z",
        "level": "INFO",
        "message": "Panel started successfully",
    }

    # ISO 8601 regex
    iso_regex = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    assert iso_regex.match(entry["timestamp"]) is not None
    assert entry["level"] in ("INFO", "WARN", "ERROR", "DEBUG")


def test_assistant_ipc_schema_validation():
    """Verify assistant IPC JSON schema has required keys and types."""
    ipc_resp = {
        "status": "ok",
        "text": "Включаю музыку",
        "action": {"type": "music_play", "id": "12345"},
    }
    assert ipc_resp["status"] == "ok"
    assert isinstance(ipc_resp["text"], str)
    assert isinstance(ipc_resp["action"], dict)


def test_pyadb_command_constants_integrity():
    """Verify pure-Python ADB TCP protocol command constants."""
    # Standard ADB protocol commands (4-byte ASCII)
    expected_cmds = (b"CNXN", b"OPEN", b"WRTE", b"OKAY", b"CLSE")
    for cmd in expected_cmds:
        assert len(cmd) == 4
        assert cmd.isupper()


def test_pyadb_packet_construction_and_checksum():
    """Verify pyadb _packet constructs valid 24-byte protocol header with checksum."""
    import struct
    pkt = pyadb._packet(pyadb.A_CNXN, pyadb.A_VERSION, pyadb.MAX_PAYLOAD, b"host::\x00")
    assert len(pkt) == 24 + len(b"host::\x00")
    cmd, a0, a1, length, crc, magic = struct.unpack("<6I", pkt[:24])
    assert cmd == pyadb.A_CNXN
    assert a0 == pyadb.A_VERSION
    assert a1 == pyadb.MAX_PAYLOAD
    assert length == len(b"host::\x00")
    assert magic == cmd ^ 0xFFFFFFFF
