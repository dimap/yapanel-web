"""
Adversarial Stress & Fuzzing Verification Suite for config.py (Milestone 1).

Rigorous empirical challenge of:
- Environment variable fuzzing (extreme numbers, negative ints, floats, booleans, invalid UTF-8/bytes)
- JSON loading fuzzing (syntax errors, empty files, non-dict roots, Unicode BOM, nonexistent/deep paths, type confusion)
- 4-Tier precedence conflict resolution (CLI > ENV > JSON > Defaults)
- Ephemeral port 0 handling across all permutations
- Path traversal deflection and resolve_paths() resilience
- Concurrency and cross-thread state isolation
"""
import argparse
import concurrent.futures
import json
import math
import os
import sys
import tempfile
from pathlib import Path
import pytest

import config
from config import (
    ConfigError,
    PanelConfig,
    ServerConfig,
    DeviceConfig,
    GlagolConfig,
    SecurityConfig,
    RemotePathsConfig,
    PartyConfig,
    AssistantConfig,
    _cast_bool,
    _cast_int,
    _cast_float,
    _cast_path,
    _cast_tuple_str,
    _cast_bytes,
    get_config,
    set_config,
)


# ============================================================================
# 1. ENVIRONMENT VARIABLE FUZZING
# ============================================================================

class TestEnvFuzzing:
    """Stress tests for environment variable parsing, casting, and resilience."""

    def test_fuzz_extreme_numbers(self):
        """Fuzz numeric environment variables with massive exponents and extreme values."""
        huge_number = "9" * 300  # 300-digit integer
        negative_huge = "-" + ("9" * 300)

        # Non-strict mode should handle huge integers or gracefully ignore them
        cfg = PanelConfig.load(env={"PANEL_PORT": huge_number, "PANEL_HTTPS_PORT": negative_huge}, strict=False)
        assert isinstance(cfg.server.port, int)
        assert cfg.server.port == int(huge_number)
        assert cfg.server.https_port == int(negative_huge)

        # Fuzz with scientific notation / infinity in integer fields
        for bad_int in ("1e10", "1e-5", "inf", "-inf", "NaN", "infinity", "10#100"):
            cfg = PanelConfig.load(env={"PANEL_PORT": bad_int}, strict=False)
            assert cfg.server.port == 8765, f"Expected default 8765 on bad int {bad_int!r}, got {cfg.server.port}"

            with pytest.raises(ConfigError):
                PanelConfig.load(env={"PANEL_PORT": bad_int}, strict=True)

    def test_fuzz_negative_integers(self):
        """Verify handling of negative integers across all integer config fields."""
        negative_env = {
            "PANEL_PORT": "-1",
            "PANEL_HTTPS_PORT": "-8766",
            "ANDROID_ADB_SERVER_PORT": "-5037",
            "YASTATION_GLAGOL_PORT": "-1961",
            "PANEL_SESSION_TTL": "-3600",
            "PANEL_PBKDF2_ITER": "-1000",
            "PANEL_MIN_PASSWORD": "-4",
            "PANEL_RATE_LIMIT_MAX_PAUSE": "-900",
            "YASTATION_AGENT_PORT": "-7861",
            "YASTATION_WAKE_PORT": "-8780",
            "YASTATION_YAM_PORT": "-8099",
            "TUYA_SOCKET_RETRY": "-5",
            "ASSISTANT_MAX_TOKENS": "-500",
        }
        cfg = PanelConfig.load(env=negative_env, strict=False)
        assert cfg.server.port == -1
        assert cfg.server.https_port == -8766
        assert cfg.device.adb_server_port == -5037
        assert cfg.glagol.port == -1961
        assert cfg.security.session_ttl_seconds == -3600
        assert cfg.security.pbkdf2_iter == -1000
        assert cfg.security.min_password_len == -4
        assert cfg.party.socket_retry == -5

    def test_fuzz_floats_in_integer_fields(self):
        """Verify that float representations in integer fields fail cleanly and fall back."""
        float_strings = ["8765.0", "8765.5", "0.0", "-1.0", "123.456", "3.14159"]
        for fstr in float_strings:
            cfg = PanelConfig.load(env={"PANEL_PORT": fstr}, strict=False)
            assert cfg.server.port == 8765

            with pytest.raises(ConfigError):
                PanelConfig.load(env={"PANEL_PORT": fstr}, strict=True)

    def test_fuzz_boolean_permutations(self):
        """Fuzz boolean fields with exhaustive casing, aliases, and adversarial inputs."""
        truthy_permutations = [
            "1", "true", "True", "TRUE", "TrUe", "tRuE",
            "yes", "Yes", "YES", "YeS",
            "on", "On", "ON",
            "enable", "Enable", "ENABLE",
            "enabled", "Enabled", "ENABLED",
        ]
        for val in truthy_permutations:
            cfg = PanelConfig.load(env={"PANEL_OPEN_BROWSER": val, "PANEL_PYADB": val, "PARTY_AUTO_START": val})
            assert cfg.server.open_browser is True, f"Failed on truthy {val!r}"
            assert cfg.device.use_pyadb is True, f"Failed on truthy {val!r}"
            assert cfg.party.auto_start is True, f"Failed on truthy {val!r}"

        falsy_permutations = [
            "0", "false", "False", "FALSE", "FaLsE",
            "no", "No", "NO",
            "off", "Off", "OFF",
            "disable", "Disable", "DISABLE",
            "disabled", "Disabled", "DISABLED",
            "", "   ", "\t",
        ]
        for val in falsy_permutations:
            cfg = PanelConfig.load(env={"PANEL_OPEN_BROWSER": val, "PANEL_PYADB": val, "PARTY_AUTO_START": val})
            assert cfg.server.open_browser is False, f"Failed on falsy {val!r}"
            assert cfg.device.use_pyadb is False, f"Failed on falsy {val!r}"
            assert cfg.party.auto_start is False, f"Failed on falsy {val!r}"

        invalid_booleans = ["invalid", "maybe", "2", "-1", "null", "none", "undefined", "true-ish", "yessir"]
        for val in invalid_booleans:
            # Non-strict mode must preserve default
            cfg = PanelConfig.load(env={"PANEL_OPEN_BROWSER": val}, strict=False)
            assert cfg.server.open_browser is False

            # Strict mode must raise ConfigError
            with pytest.raises(ConfigError):
                PanelConfig.load(env={"PANEL_OPEN_BROWSER": val}, strict=True)

    def test_fuzz_invalid_utf8_and_binary_data(self):
        """Fuzz bytes and string casting with binary, surrogate, and null bytes."""
        # Binary data in lyrics key
        binary_lyrics = b"\x00\x01\x02\xff\xfe\xfd\x80\x90\xaa"
        cfg = PanelConfig()
        cfg.glagol.lyrics_key = binary_lyrics
        d = cfg.to_dict()
        assert isinstance(d["glagol"]["lyrics_key"], str)
        assert "\ufffd" in d["glagol"]["lyrics_key"]  # utf-8 replacement char used cleanly

        # Cast bytes helper
        assert _cast_bytes("normal_str") == b"normal_str"
        assert _cast_bytes(b"already_bytes") == b"already_bytes"
        assert _cast_bytes(12345) == b"12345"

        # Env casting with null bytes
        cfg_env = PanelConfig.load(env={"YASTATION_LYRICS_KEY": "secret\x00key\x00data"}, strict=False)
        assert cfg_env.glagol.lyrics_key == b"secret\x00key\x00data"

    def test_fuzz_extreme_length_and_whitespace(self):
        """Test resilience against 10,000-character strings and aggressive whitespace."""
        giant_string = "A" * 10000
        cfg = PanelConfig.load(env={"PANEL_HOST": giant_string, "PANEL_PORT": "   8999 \t\n  "})
        assert cfg.server.host == giant_string
        assert cfg.server.port == 8999

        # Whitespace-only integer
        cfg_ws = PanelConfig.load(env={"PANEL_PORT": "   \n\t  "}, strict=False)
        assert cfg_ws.server.port == 8765


# ============================================================================
# 2. JSON LOADING & TAMPERING FUZZING
# ============================================================================

class TestJsonLoadingFuzzing:
    """Stress tests for .panel_settings.json corruption and hostile payloads."""

    def test_fuzz_corrupted_json_syntax(self):
        """Verify unhandled crash immunity on corrupted JSON syntax."""
        corrupted_payloads = [
            b"",                                        # completely empty (0 bytes)
            b"   \r\n\t   ",                            # whitespace only
            b"{",                                       # incomplete root
            b"{\"server\": {",                          # unclosed section
            b"{\"server\": {\"port\": 9000,}}",         # trailing comma
            b"{'server': {'port': 9000}}",              # single quotes
            b"{server: {port: 9000}}",                  # unquoted keys
            b"{\"server\": {\"port\": 0x1234}}",        # hex literal
            b"<<<NOT_JSON_XML>>>",                      # XML garbage
            b"\x00\x01\x02\x03\xff\xfe",                # pure binary bytes
        ]
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"

            for payload in corrupted_payloads:
                settings_file.write_bytes(payload)

                # Non-strict must never crash unhandled and must fall back to defaults
                cfg = PanelConfig.load(settings_path=tdp, env={}, strict=False)
                assert cfg.server.port == 8765
                assert cfg.server.host == "127.0.0.1"

                # Strict mode must raise ConfigError
                with pytest.raises(ConfigError):
                    PanelConfig.load(settings_path=tdp, env={}, strict=True)

    def test_fuzz_json_type_confusion_vulnerability(self):
        """
        Verify type safety in JSON loading:
        Validates that incoming string ports are coerced to int, string booleans to bool,
        and uncastable hostile structures (lists/dicts in integer fields) are skipped in non-strict mode
        and raise ConfigError in strict mode.
        """
        hostile_types_payload = {
            "server": {
                "port": "9999",              # String in integer field -> coerced to int
                "https_port": [8766],        # List in integer field -> uncastable, skipped
                "open_browser": "false",     # String 'false' in boolean field -> coerced to False
            },
            "security": {
                "min_password_len": {"dict": 1},  # Dict in integer field -> uncastable, skipped
            }
        }
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"
            settings_file.write_text(json.dumps(hostile_types_payload), encoding="utf-8")

            cfg = PanelConfig.load(settings_path=tdp, env={}, strict=False)
            assert isinstance(cfg.server.port, int)
            assert cfg.server.port == 9999
            assert isinstance(cfg.server.https_port, int)
            assert cfg.server.https_port == 8766
            assert isinstance(cfg.server.open_browser, bool)
            assert cfg.server.open_browser is False
            assert isinstance(cfg.security.min_password_len, int)
            assert cfg.security.min_password_len == 4

            with pytest.raises(ConfigError):
                PanelConfig.load(settings_path=tdp, env={}, strict=True)

    def test_fuzz_non_dict_json_roots(self):
        """Verify that non-dict JSON roots (arrays, scalars, null) are safely ignored."""
        non_dict_roots = [
            json.dumps([]),
            json.dumps([1, 2, 3, {"server": {"port": 9999}}]),
            json.dumps("string payload"),
            json.dumps(12345),
            json.dumps(True),
            json.dumps(False),
            json.dumps(None),
        ]
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"

            for payload in non_dict_roots:
                settings_file.write_text(payload, encoding="utf-8")
                cfg = PanelConfig.load(settings_path=tdp, env={}, strict=False)
                assert cfg.server.port == 8765
                assert cfg.server.host == "127.0.0.1"

    def test_fuzz_unicode_bom_in_json(self):
        """Verify transparent handling of UTF-8 files containing a Byte Order Mark (BOM)."""
        bom_payload = b"\xef\xbb\xbf{\"server\": {\"port\": 9444}}"
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"
            settings_file.write_bytes(bom_payload)

            # With utf-8-sig, BOM is transparently stripped in both non-strict and strict modes
            cfg = PanelConfig.load(settings_path=tdp, env={}, strict=False)
            assert cfg.server.port == 9444

            cfg_strict = PanelConfig.load(settings_path=tdp, env={}, strict=True)
            assert cfg_strict.server.port == 9444

    def test_fuzz_deeply_nested_and_unexpected_keys(self):
        """Verify tolerance to unknown sections, unknown keys, and type mismatches in JSON."""
        hostile_payload = {
            "unknown_section": {"hacker_key": "injected_value", "nested": {"deep": True}},
            "server": {
                "port": 9555,
                "unexpected_field": "ignore_me",
                "open_browser": True,
            },
            "device": "legacy_flat_device_serial:5555",
            "glagol": "not_a_dict_string_so_ignore",
            "security": ["not", "a", "dict"],
            "remote": None,
        }
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"
            settings_file.write_text(json.dumps(hostile_payload), encoding="utf-8")

            cfg = PanelConfig.load(settings_path=tdp, env={}, strict=False)
            assert cfg.server.port == 9555
            assert cfg.server.open_browser is True
            assert cfg.device.serial == "legacy_flat_device_serial:5555"
            assert cfg.glagol.port == 1961  # default intact because string glagol was skipped
            assert cfg.security.session_ttl_seconds == 7776000  # default intact

    def test_nonexistent_and_directory_settings_path(self):
        """Verify non-existent path or directory path behavior."""
        missing = Path("completely_non_existent_folder_abc123/settings.json")
        cfg = PanelConfig.load(settings_path=missing, env={}, strict=False)
        assert cfg.server.port == 8765
        assert cfg.server.data_dir.name == "completely_non_existent_folder_abc123"


# ============================================================================
# 3. 4-TIER PRECEDENCE & CONFLICT RESOLUTION
# ============================================================================

class TestPrecedenceConflicts:
    """Stress tests verifying strict 4-tier precedence: CLI > ENV > JSON > Defaults."""

    def test_complete_4_tier_conflict_resolution(self):
        """Exhaustive check: CLI overrides ENV, ENV overrides JSON, JSON overrides Defaults."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"
            settings_file.write_text(json.dumps({"server": {"port": 7001, "host": "10.0.0.1"}}), encoding="utf-8")

            # All 4 tiers active: CLI must win
            cfg1 = PanelConfig.load(
                settings_path=tdp,
                env={"PANEL_PORT": "7002", "PANEL_HOST": "10.0.0.2"},
                cli_args={"port": 7003, "host": "10.0.0.3"},
            )
            assert cfg1.server.port == 7003
            assert cfg1.server.host == "10.0.0.3"

            # Remove CLI: ENV must win
            cfg2 = PanelConfig.load(
                settings_path=tdp,
                env={"PANEL_PORT": "7002", "PANEL_HOST": "10.0.0.2"},
                cli_args=None,
            )
            assert cfg2.server.port == 7002
            assert cfg2.server.host == "10.0.0.2"

            # Remove ENV: JSON must win
            cfg3 = PanelConfig.load(
                settings_path=tdp,
                env={},
                cli_args=None,
            )
            assert cfg3.server.port == 7001
            assert cfg3.server.host == "10.0.0.1"

            # Remove JSON: Defaults must win
            settings_file.unlink()
            cfg4 = PanelConfig.load(
                settings_path=tdp,
                env={},
                cli_args=None,
            )
            assert cfg4.server.port == 8765
            assert cfg4.server.host == "127.0.0.1"

    def test_cli_none_values_do_not_override_env_or_json(self):
        """Verify that argparse defaults of None do not stomp ENV or JSON values."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"
            settings_file.write_text(json.dumps({"server": {"host": "192.168.1.100"}}), encoding="utf-8")

            # CLI has port=None and host=None
            cli_dict = {"port": None, "host": None, "device": None}
            cfg = PanelConfig.load(
                settings_path=tdp,
                env={"PANEL_PORT": "8900"},
                cli_args=cli_dict,
            )
            # Host should come from JSON (since CLI is None)
            assert cfg.server.host == "192.168.1.100"
            # Port should come from ENV (since CLI is None)
            assert cfg.server.port == 8900

    def test_argparse_namespace_integration(self):
        """Verify that standard argparse.Namespace behaves identically to dictionary."""
        ns = argparse.Namespace(
            host="127.0.0.2",
            port=9200,
            https_port=None,
            data_dir=None,
            device="192.168.1.50:5555",
            model="mini3",
            find=None,
            open=True,
        )
        cfg = PanelConfig.load(
            env={"PANEL_PORT": "8888", "PANEL_OPEN_BROWSER": "0"},
            cli_args=ns,
        )
        assert cfg.server.host == "127.0.0.2"
        assert cfg.server.port == 9200
        assert cfg.device.serial == "192.168.1.50:5555"
        assert cfg.device.model == "mini3"
        assert cfg.server.open_browser is True

    def test_cli_open_store_true_default_does_not_stomp_env(self):
        """Verify that argparse store_true default False does not overwrite PANEL_OPEN_BROWSER=1."""
        ap = argparse.ArgumentParser()
        ap.add_argument("--open", action="store_true")
        args = ap.parse_args([])
        assert args.open is False

        # CLI args default False should not stomp PANEL_OPEN_BROWSER=1
        cfg = PanelConfig.load(cli_args=args, env={"PANEL_OPEN_BROWSER": "1"})
        assert cfg.server.open_browser is True

        # Explicitly setting True in CLI overrides ENV 0
        cfg_explicit = PanelConfig.load(cli_args={"open": True}, env={"PANEL_OPEN_BROWSER": "0"})
        assert cfg_explicit.server.open_browser is True


# ============================================================================
# 4. PORT 0 & HTTPS-PORT 0 PERMUTATIONS (EPHEMERAL BINDING)
# ============================================================================

class TestPortZeroPermutations:
    """Stress tests ensuring port 0 is preserved as int 0 and not falsified."""

    def test_all_port_zero_combinations(self):
        """Verify port=0 and https_port=0 under all tier combinations."""
        # 1. CLI port=0, https_port=0 overriding non-zero ENV & JSON
        cfg1 = PanelConfig.load(
            cli_args={"port": 0, "https_port": 0},
            env={"PANEL_PORT": "8765", "PANEL_HTTPS_PORT": "8766"},
        )
        assert cfg1.server.port == 0
        assert cfg1.server.https_port == 0

        # 2. CLI port=0, https_port not specified -> https_port comes from ENV
        cfg2 = PanelConfig.load(
            cli_args={"port": 0, "https_port": None},
            env={"PANEL_PORT": "8765", "PANEL_HTTPS_PORT": "8766"},
        )
        assert cfg2.server.port == 0
        assert cfg2.server.https_port == 8766

        # 3. CLI port not specified, https_port=0 -> port comes from ENV, https_port is 0
        cfg3 = PanelConfig.load(
            cli_args={"port": None, "https_port": 0},
            env={"PANEL_PORT": "8765", "PANEL_HTTPS_PORT": "8766"},
        )
        assert cfg3.server.port == 8765
        assert cfg3.server.https_port == 0

        # 4. ENV has port=0, https_port=0 overriding JSON
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            settings_file = tdp / ".panel_settings.json"
            settings_file.write_text(json.dumps({"server": {"port": 9000, "https_port": 9001}}), encoding="utf-8")

            cfg4 = PanelConfig.load(
                settings_path=tdp,
                env={"PANEL_PORT": "0", "PANEL_HTTPS_PORT": "0"},
            )
            assert cfg4.server.port == 0
            assert cfg4.server.https_port == 0

            # 5. JSON has port=0, https_port=0 overriding Defaults
            settings_file.write_text(json.dumps({"server": {"port": 0, "https_port": 0}}), encoding="utf-8")
            cfg5 = PanelConfig.load(settings_path=tdp, env={})
            assert cfg5.server.port == 0
            assert cfg5.server.https_port == 0

    def test_cli_string_zero_casting(self):
        """Verify passing '0' as a string in CLI args dictionary casts cleanly to int 0."""
        cfg = PanelConfig.load(cli_args={"port": "0", "https_port": "0"})
        assert cfg.server.port == 0
        assert type(cfg.server.port) is int
        assert cfg.server.https_port == 0
        assert type(cfg.server.https_port) is int


# ============================================================================
# 5. PATH TRAVERSAL & RESOLVE_PATHS() BEHAVIOR
# ============================================================================

class TestPathTraversalAndResolution:
    """Stress tests on directory path resolution, dot-dot traversal, and filesystem edges."""

    def test_path_traversal_in_data_dir(self):
        """Verify data_dir dot-dot relative traversal resolves cleanly to absolute path."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            sub = base / "a" / "b" / "c"
            sub.mkdir(parents=True)

            traversal_path = sub / ".." / ".." / "escaped_dir"
            cfg = PanelConfig()
            cfg.server.data_dir = traversal_path
            cfg.resolve_paths()

            # Path must be resolved to absolute canonical path
            assert cfg.server.data_dir.is_absolute()
            assert ".." not in cfg.server.data_dir.parts
            assert cfg.server.data_dir.name == "escaped_dir"
            assert cfg.server.data_dir.is_dir()

            # Child properties must all be children of resolved data_dir
            assert cfg.server.auth_file == cfg.server.data_dir / ".yandex_auth.json"
            assert cfg.server.panel_auth_file == cfg.server.data_dir / ".panel_auth.json"
            assert cfg.server.metrics_file == cfg.server.data_dir / ".metrics.json"
            assert cfg.server.tls_dir == cfg.server.data_dir / ".tls"
            assert cfg.server.settings_file == cfg.server.data_dir / ".panel_settings.json"
            assert cfg.server.agent_file == cfg.server.data_dir / ".agent.json"
            assert cfg.server.journal_file == cfg.server.data_dir / ".panel_journal.jsonl"

    def test_resolve_paths_when_data_dir_is_existing_file(self):
        """Verify resolve_paths() does not crash if data_dir points to an existing file."""
        with tempfile.NamedTemporaryFile() as tf:
            file_path = Path(tf.name)
            cfg = PanelConfig()
            cfg.server.data_dir = file_path
            # Calling resolve_paths() attempts mkdir(exist_ok=True) which raises FileExistsError (subclass of OSError).
            # Must catch OSError cleanly without crashing!
            cfg.resolve_paths()
            assert cfg.server.data_dir == file_path.resolve()

    def test_cast_path_expands_user(self):
        """Verify _cast_path expands '~' and preserves Path instances."""
        p = _cast_path("~")
        assert p == Path("~").expanduser()
        p2 = _cast_path(Path("/tmp/foo"))
        assert p2 == Path("/tmp/foo")


# ============================================================================
# 6. CONCURRENCY & STATE CORRUPTION STRESS
# ============================================================================

class TestConcurrencyAndStateIsolation:
    """Stress tests on multi-threaded loading and singleton isolation."""

    def test_concurrent_panel_config_load(self):
        """Run 50 parallel threads loading configurations concurrently."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)

            def worker(idx: int):
                local_env = {
                    "PANEL_PORT": str(9000 + idx),
                    "PANEL_HOST": f"10.0.0.{idx % 250}",
                    "PANEL_OPEN_BROWSER": "1" if idx % 2 == 0 else "0",
                }
                local_cfg = PanelConfig.load(settings_path=tdp, env=local_env, strict=False)
                assert local_cfg.server.port == 9000 + idx
                assert local_cfg.server.host == f"10.0.0.{idx % 250}"
                assert local_cfg.server.open_browser is (idx % 2 == 0)
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                futures = [executor.submit(worker, i) for i in range(50)]
                for f in concurrent.futures.as_completed(futures):
                    assert f.result() is True

    def test_instance_mutation_isolation(self):
        """Verify mutating one instance does not mutate default factories of other instances."""
        cfg1 = PanelConfig()
        cfg1.server.allow_networks = ("192.168.1.0/24",)
        cfg1.server.port = 1111

        cfg2 = PanelConfig()
        assert cfg2.server.port == 8765
        assert cfg2.server.allow_networks == ()
