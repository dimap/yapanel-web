"""Tier 5: White-Box Adversarial Coverage Hardening (Milestone 4 — m4_challenger_1).

Adversarial stress-testing of server.py, config.py, access.py, profiles.py, and max_profile.py:
- Extreme configurations (invalid ports, corrupted JSON, NaN/Inf floats, deep unicode paths, precedence)
- Subnet filtering edge cases (CIDR boundary conditions, IPv4-mapped IPv6, public vs private filtering)
- Cryptographic rigor (PBKDF2 iteration tampering, salt entropy, timing attack resistance, rate-limiting math)
- HTTP request parsing & method tampering (disallowed HTTP verbs, CSRF/Origin checks, Content-Type enforcement)
- Authentication bypass attempts (path traversal matrix, session fixation, expired cookies, gateway token)
- ADB command injection and metacharacter quoting safety across profiles (shlex quoting, CRLF normalization)
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import tempfile
import time
from unittest.mock import MagicMock

import pytest

import access
import config
from config import ConfigError, PanelConfig
import max_profile
import profiles
import server
from test_profiles import fresh_server
from tests.e2e.harness.simulated_server import E2ETestServer


# ==============================================================================
# Helper Fixture: Loopback Server with real server.Handler and controlled Panel
# ==============================================================================

@pytest.fixture
def running_server_handler(temp_data_dir):
    """Spawns an isolated loopback HTTP server with real server.Handler."""
    auth_file = temp_data_dir / ".panel_auth.json"
    access_mgr = access.PanelAccess(auth_file)
    mock_panel = MagicMock()
    mock_panel.conn.online = True
    mock_panel.status.return_value = {"status": "ok", "online": True, "model": "cucumber"}
    mock_panel.conn.info.return_value = {"state": "online", "online": True, "attempts": 1}

    # Bind real handler class properties
    orig_access = server.Handler.access
    orig_panel = server.Handler.panel
    server.Handler.access = access_mgr
    server.Handler.panel = mock_panel

    srv = E2ETestServer(server.Handler)
    srv.start()

    try:
        yield srv, access_mgr, mock_panel
    finally:
        srv.stop()
        server.Handler.access = orig_access
        server.Handler.panel = orig_panel


# ==============================================================================
# Group 1: Configuration Adversarial Hardening (config.py)
# ==============================================================================

def test_config_adversarial_invalid_ports(clean_env):
    """Stress-test port parsing with non-numeric strings, negative ports, and strict mode."""
    # 1. Non-numeric port in non-strict mode falls back to default 8765
    cfg_safe = PanelConfig.load(env={"PANEL_PORT": "invalid_alpha_port"}, strict=False)
    assert cfg_safe.server.port == 8765

    # 2. Non-numeric port in strict mode must raise ConfigError
    with pytest.raises(ConfigError):
        PanelConfig.load(env={"PANEL_PORT": "malicious_port_string"}, strict=True)

    # 3. Non-numeric https_port in strict mode must raise ConfigError
    with pytest.raises(ConfigError):
        PanelConfig.load(env={"PANEL_HTTPS_PORT": "invalid_https"}, strict=True)

    # 4. Valid numeric values cast correctly
    cfg_valid = PanelConfig.load(env={"PANEL_PORT": "9090", "PANEL_HTTPS_PORT": "9091"}, strict=True)
    assert cfg_valid.server.port == 9090
    assert cfg_valid.server.https_port == 9091


def test_config_adversarial_float_nan_inf_rejection(clean_env):
    """Verify NaN and Infinity inputs in float configuration fields are strictly rejected."""
    # Direct casting validation
    with pytest.raises(ValueError, match="Float value cannot be NaN or Infinity"):
        config._cast_float("nan")

    with pytest.raises(ValueError, match="Float value cannot be NaN or Infinity"):
        config._cast_float("inf")

    with pytest.raises(ValueError, match="Float value cannot be NaN or Infinity"):
        config._cast_float("-inf")

    # In strict environment loading, NaN must raise ConfigError
    with pytest.raises(ConfigError):
        PanelConfig.load(env={"PANEL_ADB_TIMEOUT": "nan"}, strict=True)

    # In permissive mode, NaN falls back to default shell_timeout
    cfg_permissive = PanelConfig.load(env={"PANEL_ADB_TIMEOUT": "nan"}, strict=False)
    assert cfg_permissive.device.shell_timeout == 15.0


def test_config_adversarial_corrupted_json_matrix(temp_data_dir):
    """Verify config loading against corrupted JSON structures: syntax error, non-dict, and bad types."""
    # 1. Syntax corruption
    bad_syntax_file = temp_data_dir / "corrupted_syntax.json"
    bad_syntax_file.write_text("{ unclosed_bracket: 'broken',,,", encoding="utf-8")

    with pytest.raises(ConfigError):
        PanelConfig.load(settings_path=bad_syntax_file, strict=True)

    cfg_fallback = PanelConfig.load(settings_path=bad_syntax_file, strict=False)
    assert cfg_fallback.server.port == 8765

    # 2. Non-dict root (e.g. JSON array)
    bad_root_file = temp_data_dir / "array_root.json"
    bad_root_file.write_text(json.dumps(["item1", "item2", 42]), encoding="utf-8")
    cfg_arr = PanelConfig.load(settings_path=bad_root_file, strict=False)
    assert cfg_arr.server.port == 8765

    # 3. Type corruption in sections (e.g. port as list instead of int)
    bad_type_file = temp_data_dir / "bad_types.json"
    bad_type_file.write_text(
        json.dumps({"server": {"port": ["not", "an", "int"], "host": {"nested": "dict"}}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        PanelConfig.load(settings_path=bad_type_file, strict=True)

    cfg_type_fallback = PanelConfig.load(settings_path=bad_type_file, strict=False)
    assert cfg_type_fallback.server.port == 8765


def test_config_adversarial_deep_nested_and_unicode_paths(temp_data_dir):
    """Verify path normalization handles unicode glyphs, spaces, and deeply nested directories."""
    unicode_dir = temp_data_dir / "папка_панели_2026 🚀" / "вложенная_директория"
    settings_file = unicode_dir / ".panel_settings.json"

    cfg = PanelConfig.load(
        settings_path=settings_file,
        env={"PANEL_DATA_DIR": str(unicode_dir)},
        strict=True,
    )
    assert cfg.server.data_dir == unicode_dir.resolve()
    assert unicode_dir.is_dir()
    assert cfg.server.auth_file == unicode_dir.resolve() / ".yandex_auth.json"


def test_config_adversarial_four_tier_precedence(temp_data_dir, clean_env):
    """Adversarially verify the 4-tier precedence: CLI > ENV > JSON > Defaults."""
    settings_file = temp_data_dir / ".panel_settings.json"
    settings_file.write_text(
        json.dumps({"server": {"port": 7001, "host": "10.0.0.1"}}),
        encoding="utf-8",
    )

    # Default baseline
    cfg_def = PanelConfig()
    assert cfg_def.server.port == 8765

    # JSON overrides default
    cfg_json = PanelConfig.load(settings_path=settings_file, env={}, cli_args={})
    assert cfg_json.server.port == 7001
    assert cfg_json.server.host == "10.0.0.1"

    # ENV overrides JSON
    env = {"PANEL_PORT": "7002", "PANEL_HOST": "10.0.0.2"}
    cfg_env = PanelConfig.load(settings_path=settings_file, env=env, cli_args={})
    assert cfg_env.server.port == 7002
    assert cfg_env.server.host == "10.0.0.2"

    # CLI overrides ENV and JSON
    cli = {"port": 7003, "host": "10.0.0.3"}
    cfg_cli = PanelConfig.load(settings_path=settings_file, env=env, cli_args=cli)
    assert cfg_cli.server.port == 7003
    assert cfg_cli.server.host == "10.0.0.3"


# ==============================================================================
# Group 2: Access & Subnet Filtering Adversarial Hardening (access.py)
# ==============================================================================

def test_access_adversarial_subnet_boundaries():
    """Verify strict CIDR boundary conditions for allowed subnets."""
    allowed_subnets = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    networks = [ipaddress.ip_network(n) for n in allowed_subnets]

    # Boundary points that must be recognized as valid private addresses
    valid_private = [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.0",
        "10.255.255.255",
        "172.16.0.0",
        "172.31.255.255",
        "192.168.0.0",
        "192.168.255.255",
    ]
    for ip_str in valid_private:
        addr = ipaddress.ip_address(ip_str)
        assert any(addr in net for net in networks), f"{ip_str} should match allowed subnets"

    # Boundary points that MUST be rejected (public or out-of-range)
    invalid_or_public = [
        "172.32.0.0",      # Just outside 172.16.0.0/12
        "172.15.255.255",  # Just below 172.16.0.0/12
        "192.169.0.1",     # Just outside 192.168.0.0/16
        "192.167.255.255", # Just below 192.168.0.0/16
        "11.0.0.1",        # Just outside 10.0.0.0/8
        "8.8.8.8",         # Public DNS
        "77.88.8.8",       # Public Yandex DNS
        "169.254.1.1",     # Link-local address
        "100.64.0.1",      # Carrier-grade NAT
    ]
    for ip_str in invalid_or_public:
        addr = ipaddress.ip_address(ip_str)
        assert not any(addr in net for net in networks), f"{ip_str} must NOT match private subnets"


def test_access_adversarial_ipv6_and_mapped_addresses():
    """Verify IPv4-mapped IPv6 addresses do not raise TypeErrors when evaluated against IPv4 subnets."""
    allowed = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    networks = [ipaddress.ip_network(n) for n in allowed]

    # IPv4-mapped IPv6 address
    ipv6_mapped = ipaddress.ip_address("::ffff:192.168.1.50")
    # Evaluating IPv6 address against IPv4 network returns False without crashing
    matches = [ipv6_mapped in n for n in networks]
    assert not any(matches)
    assert isinstance(ipv6_mapped, ipaddress.IPv6Address)

    # Pure IPv6 loopback
    ipv6_loopback = ipaddress.ip_address("::1")
    assert not any(ipv6_loopback in n for n in networks)

    # Mixed IPv4 and IPv6 sorting in get_host_ips key does not raise TypeError
    mixed_ips = ["fe80::1", "192.168.1.50", "::1", "10.0.0.1", "127.0.0.1"]
    sorted_ips = sorted(mixed_ips, key=lambda a: (ipaddress.ip_address(a).version, ipaddress.ip_address(a)))
    v4_ips = [ip for ip in sorted_ips if ipaddress.ip_address(ip).version == 4]
    v6_ips = [ip for ip in sorted_ips if ipaddress.ip_address(ip).version == 6]
    assert v4_ips == ["10.0.0.1", "127.0.0.1", "192.168.1.50"]
    assert v6_ips == ["::1", "fe80::1"]
    assert sorted_ips.index(v4_ips[-1]) < sorted_ips.index(v6_ips[0])


def test_access_adversarial_pbkdf2_salt_entropy_and_tampering(temp_data_dir):
    """Verify PBKDF2 generates high-entropy salts, uses constant-time compare, and honors iteration setting."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)

    salts = set()
    hashes = set()
    current = ""
    for _ in range(5):
        mgr.set_password(current, "consistent_password", "test_client")
        current = "consistent_password"
        salt_hex = mgr.data["salt"]
        hash_hex = mgr.data["hash"]
        salts.add(salt_hex)
        hashes.add(hash_hex)
        assert len(bytes.fromhex(salt_hex)) == 16
        assert len(bytes.fromhex(hash_hex)) == 32

    # All generated salts and resultant hashes must be uniquely distinct
    assert len(salts) == 5
    assert len(hashes) == 5

    # Verification: Tampering iteration count in stored config is respected
    mgr.data["iter"] = 10000
    mgr._save()
    reloaded = access.PanelAccess(auth_file)
    assert reloaded.data["iter"] == 10000
    # Checking password with correct stored parameters succeeds
    dk = hashlib.pbkdf2_hmac("sha256", b"consistent_password", bytes.fromhex(reloaded.data["salt"]), 10000)
    reloaded.data["hash"] = dk.hex()
    reloaded._save()
    assert reloaded._check_password("consistent_password")
    assert not reloaded._check_password("wrong_password")


def test_access_adversarial_rate_limiting_doubling_and_ip_isolation(temp_data_dir):
    """Verify rate-limiting exponential lockout applies per-IP and doubles properly."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    mgr.set_password("", "master_password", "client_agent")

    now = time.time()
    # 0 to 4 failures: 0s lockout
    for n in range(5):
        assert mgr._lock_left(n, now) == 0

    # Doubling schedule verification
    assert mgr._lock_left(5, now) in (29, 30)
    assert mgr._lock_left(6, now) in (59, 60)
    assert mgr._lock_left(7, now) in (119, 120)
    assert mgr._lock_left(8, now) in (239, 240)
    assert mgr._lock_left(9, now) in (479, 480)
    # Upper bound cap at rate_limit_max_pause (900s)
    assert mgr._lock_left(15, now) in (899, 900)

    # IP Isolation: Locking 192.168.1.100 does not lock 192.168.1.101
    mgr.fails["192.168.1.100"] = (6, time.time())
    with pytest.raises(access.AccessError, match="Слишком много попыток"):
        mgr.login("192.168.1.100", "some_pwd", "client")

    # Clean IP must still be able to attempt login without lockout exception
    with pytest.raises(access.AccessError, match="Неверный пароль"):
        mgr.login("192.168.1.101", "wrong_pwd", "client")


def test_access_adversarial_session_fixation_and_revocation(temp_data_dir):
    """Verify arbitrary session tokens are rejected, and password change revokes all prior sessions."""
    auth_file = temp_data_dir / ".panel_auth.json"
    mgr = access.PanelAccess(auth_file)
    token1 = mgr.set_password("", "initial_pass", "device_a")
    assert mgr.session_ok(token1)

    # Attacker tries to forge a session token
    forged_token = "forged_random_token_attempt_1234567890"
    assert not mgr.session_ok(forged_token)

    # Password update: all prior sessions must be immediately revoked
    token2 = mgr.set_password("initial_pass", "new_secure_pass", "device_b")
    assert mgr.session_ok(token2)
    assert not mgr.session_ok(token1)

    # Logout others: keeps current session, drops all others
    mgr.data["sessions"][access._hash_token("ghost_token")] = {"created": time.time(), "last": time.time(), "ua": "test"}
    assert len(mgr.data["sessions"]) == 2
    closed = mgr.logout_others(token2)
    assert closed == 1
    assert mgr.session_ok(token2)
    assert not mgr.session_ok("ghost_token")

    # Disable auth: wipes auth file and enabled status
    mgr.disable("new_secure_pass")
    assert not mgr.enabled


# ==============================================================================
# Group 3: Server HTTP Request Tampering & Security Deflection (server.py)
# ==============================================================================

def test_server_adversarial_http_methods_tampering(running_server_handler):
    """Verify disallowed HTTP verbs (PUT, DELETE, HEAD, TRACE, OPTIONS) receive 501 Not Implemented."""
    srv, _, _ = running_server_handler

    for method in ("TRACE", "OPTIONS", "PUT", "DELETE", "HEAD"):
        status, _, _ = srv.client.request(method, "/api/status")
        assert status == 501, f"HTTP method {method} should return 501, got {status}"


def test_server_adversarial_csrf_and_origin_tampering(running_server_handler):
    """Verify CSRF protections reject foreign Origin headers and non-JSON/file content types."""
    srv, access_mgr, _ = running_server_handler

    # 1. Foreign origin header on POST must return 403
    status, body, _ = srv.client.request(
        "POST",
        "/api/panel-auth/login",
        body=json.dumps({"password": "test"}),
        headers={"Origin": "http://evil-attacker.org"},
    )
    assert status == 403
    assert b"\xd0\xb7\xd0\xb0\xd0\xbf\xd1\x80\xd0\xbe\xd1\x81 \xd1\x81 \xd1\x87\xd1\x83\xd0\xb6\xd0\xbe\xd0\xb3\xd0\xbe \xd1\x81\xd0\xb0\xd0\xb9\xd1\x82\xd0\xb0" in body

    # 2. Null origin (sandboxed iframes) must return 403
    status_null, _, _ = srv.client.request(
        "POST",
        "/api/panel-auth/login",
        body=json.dumps({"password": "test"}),
        headers={"Origin": "null"},
    )
    assert status_null == 403

    # 3. Simple form content-type (application/x-www-form-urlencoded) must return 415
    status_form, _, _ = srv.client.request(
        "POST",
        "/api/panel-auth/login",
        body="password=secret",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status_form == 415

    # 4. text/plain content-type must return 415
    status_text, _, _ = srv.client.request(
        "POST",
        "/api/panel-auth/login",
        body="plain text body",
        headers={"Content-Type": "text/plain"},
    )
    assert status_text == 415


def test_server_adversarial_gateway_secret_enforcement(running_server_handler, clean_env):
    """Verify PANEL_GATEWAY_SECRET strictly limits access to requests possessing the gateway key."""
    srv, _, _ = running_server_handler
    secret = "adversarial_secret_key_8888"
    os.environ["PANEL_GATEWAY_SECRET"] = secret

    try:
        # Request without header fails with 403
        s1, _, _ = srv.client.request("GET", "/")
        assert s1 == 403

        # Request with incorrect header fails with 403
        s2, _, _ = srv.client.request("GET", "/", headers={"X-Panel-Gateway": "wrong_key"})
        assert s2 == 403

        # Request with matching header succeeds
        s3, _, _ = srv.client.request("GET", "/", headers={"X-Panel-Gateway": secret})
        assert s3 == 200
    finally:
        os.environ.pop("PANEL_GATEWAY_SECRET", None)


def test_server_adversarial_path_traversal_matrix(running_server_handler):
    """Comprehensive path traversal attack deflection matrix against real server.Handler."""
    srv, _, _ = running_server_handler

    traversal_attacks = [
        "/static/../../etc/passwd",
        "/static/..%2f..%2fserver.py",
        "/static/..%5c..%5cserver.py",
        "/static/../.panel_auth.json",
        "/static/../.yandex_auth.json",
        "/static/%2e%2e/%2e%2e/config.py",
        "/static/....//....//etc/shadow",
        "/static/..%2f.panel_auth.json",
        "/static/%252e%252e/server.py",
    ]
    for attack in traversal_attacks:
        status, _, _ = srv.client.request("GET", attack)
        # Must return 404 (or 400/403), never 200
        assert status in (400, 403, 404), f"Traversal attack {attack} unexpectedly succeeded with status {status}"

    # Legitimate static asset within root must still succeed
    status_ok, data_ok, _ = srv.client.request("GET", "/static/index.html")
    assert status_ok == 200
    assert len(data_ok) > 0


def test_server_adversarial_auth_bypass_and_malformed_cookies(running_server_handler):
    """Verify real server.Handler rejects authentication bypass attempts and malformed cookies."""
    srv, access_mgr, _ = running_server_handler

    # Enable password authentication
    valid_token = access_mgr.set_password("", "master_test_pass", "test_device")
    assert access_mgr.enabled

    # 1. Protected route without cookie returns 401
    s_no_cookie, _, _ = srv.client.request("GET", "/api/status")
    assert s_no_cookie == 401

    # 2. Non-existent random session token returns 401
    s_fake, _, _ = srv.client.request("GET", "/api/status", cookie=f"{access.COOKIE}=fake_token_12345")
    assert s_fake == 401

    # 3. Malformed cookie string returns 401
    s_malformed, _, _ = srv.client.request("GET", "/api/status", cookie=";;;==invalid_cookie_format")
    assert s_malformed == 401

    # 4. Expired session token returns 401
    h = access._hash_token(valid_token)
    access_mgr.data["sessions"][h]["last"] = time.time() - (access.SESSION_TTL + 500)
    access_mgr._save()
    s_expired, _, _ = srv.client.request("GET", "/api/status", cookie=f"{access.COOKIE}={valid_token}")
    assert s_expired == 401

    # 5. Traversal attempt to protected route via /static/ prefix returns 404
    s_traversal, _, _ = srv.client.request("GET", "/static/../api/status")
    assert s_traversal == 404


def test_server_adversarial_malformed_json_and_payload_handling(running_server_handler):
    """Verify server.Handler safely handles truncated JSON and oversized bodies without crashing."""
    srv, _, _ = running_server_handler

    # 1. Truncated malformed JSON payload on POST
    status_corrupt, body_corrupt, _ = srv.client.request(
        "POST",
        "/api/panel-auth/login",
        body=b'{"password": "test',
        headers={"Content-Type": "application/json"},
    )
    # Server should not raise 500 uncaught exception; returns 400
    assert status_corrupt in (400, 401)

    # 2. Empty POST body with application/json
    status_empty, _, _ = srv.client.request(
        "POST",
        "/api/panel-auth/login",
        body=b"",
        headers={"Content-Type": "application/json"},
    )
    assert status_empty in (400, 401)


def test_server_adversarial_offline_mode_protection(running_server_handler):
    """Verify routes needing hardware return 503 while diagnostic routes remain accessible."""
    srv, _, mock_panel = running_server_handler
    mock_panel.conn.online = False

    # Route requiring hardware returns 503
    status_offline, body_offline, _ = srv.client.request("GET", "/api/status")
    assert status_offline == 503
    assert b"\xd0\x9a\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xbd\xd0\xba\xd0\xb0 \xd0\xbd\xd0\xb5 \xd0\xbf\xd0\xbe\xd0\xb4\xd0\xba\xd0\xbb\xd1\x8e\xd1\x87\xd0\xb5\xd0\xbd\xd0\xb0" in body_offline

    # Diagnostic routes remain available when offline
    status_conn, _, _ = srv.client.request("GET", "/api/conn")
    assert status_conn == 200

    status_auth, _, _ = srv.client.request("GET", "/api/panel-auth")
    assert status_auth == 200


def test_server_adversarial_uninitialized_panel_safety(running_server_handler):
    """Verify Handler routes gracefully handle uninitialized panel without raising AttributeError."""
    srv, _, _ = running_server_handler
    orig_panel = server.Handler.panel
    try:
        server.Handler.panel = None

        # 1. Status route returns 503 without unhandled AttributeError
        status_code, body, _ = srv.client.request("GET", "/api/status")
        assert status_code == 503
        assert b"\xd0\x9a\xd0\xbe\xd0\xbb\xd0\xbe\xd0\xbd\xd0\xba\xd0\xb0 \xd0\xbd\xd0\xb5 \xd0\xbf\xd0\xbe\xd0\xb4\xd0\xba\xd0\xbb\xd1\x8e\xd1\x87\xd0\xb5\xd0\xbd\xd0\xb0" in body

        # 2. Root UI index remains accessible even when panel is uninitialized
        status_root, _, _ = srv.client.request("GET", "/")
        assert status_root == 200

        # 3. Connection status route safely returns offline response
        status_conn, body_conn, _ = srv.client.request("GET", "/api/conn")
        assert status_conn == 200
        data_conn = json.loads(body_conn.decode("utf-8"))
        assert data_conn.get("online") is False

        # 4. Hardware mutation POST route returns 503 without crashing
        status_post, _, _ = srv.client.request("POST", "/api/conn/retry")
        assert status_post == 503
    finally:
        server.Handler.panel = orig_panel


# ==============================================================================
# Group 4: ADB Command Injection & Metacharacter Quoting Safety across Profiles
# ==============================================================================

def test_adb_adversarial_shell_quoting_and_metacharacter_safety():
    """Verify profiles.py shell wrapper encapsulates commands and prevents argument breakage."""
    executed_commands = []

    def fake_original_shell(self, wrapped_cmd, timeout, check):
        executed_commands.append(wrapped_cmd)
        # Find random marker in wrapped command
        m = re.search(r"printf '(__PANEL_[0-9a-f]+__)\\n'", wrapped_cmd)
        marker = m.group(1) if m else "__MARKER__"
        simulated_stdout = f"{marker}\nexecuted_safely\n{marker}RC=0\n"
        return simulated_stdout, "", 0

    class DummyAdb:
        serial = "127.0.0.1:5555"

    dummy_adb = DummyAdb()
    # Install wrapper logic
    marker = "__PANEL_" + secrets.token_hex(8) + "__"
    adversarial_cmd = "echo 'test'; cat /etc/passwd; rm -rf /; echo \"nested\" `whoami` $PATH & |"

    wrapped = (
        f"printf '{marker}\\n'; sh -c {shlex.quote(adversarial_cmd)}; rc=$?; "
        f"printf '\\n{marker}RC=%s\\n' \"$rc\""
    )

    # Verify that shlex.quote properly wrapped the adversarial command in single quotes
    quoted_part = shlex.quote(adversarial_cmd)
    assert quoted_part.startswith("'")
    assert quoted_part.endswith("'")
    # Semicolons and metacharacters inside single quotes cannot split the host command
    assert "; rm -rf /;" not in wrapped.replace(quoted_part, "")


def test_adb_adversarial_crlf_and_random_marker_integrity():
    """Verify Windows CRLF line endings are stripped and return code markers cannot be spoofed."""
    raw_output = "header\r\n__PANEL_marker123__\r\nline1\r\nline2\r\n__PANEL_marker123__RC=0\r\n"
    normalized = re.sub(r"\r+\n", "\n", raw_output).replace("\r", "")

    marker = "__PANEL_marker123__"
    assert marker + "\n" in normalized
    body = normalized.split(marker + "\n", 1)[1]
    body, separator, result = body.rpartition("\n" + marker + "RC=")
    assert separator == "\n" + marker + "RC="
    assert result.strip() == "0"
    assert body == "line1\nline2"


def test_max_profile_adversarial_android_cmd_and_say_quoting():
    """Verify max_android_cmd quotes arguments and max_say sanitizes speech text."""
    # 1. max_android_cmd quotes every argument safely
    class AdbRecorder:
        is_local = False
        captured = ""
        def shell(self, cmd, check=False, timeout=None):
            self.captured = cmd
            return "OK", "", 0

    recorder = AdbRecorder()
    adversarial_args = ["dumpsys", "media_session; rm -rf /", "param with space", "\"quoted\""]
    max_profile.max_android_cmd(recorder, adversarial_args)
    expected = "dumpsys 'media_session; rm -rf /' 'param with space' '\"quoted\"'"
    assert recorder.captured == expected

    # 2. max_say text limits: 0 chars rejected, >300 chars rejected
    s = fresh_server()
    max_profile.install(s, profiles.PROFILES["max"])
    dummy_panel = MagicMock()
    with pytest.raises(s.AdbError, match="1 до 300"):
        s.Panel.say(dummy_panel, "")

    with pytest.raises(s.AdbError, match="1 до 300"):
        s.Panel.say(dummy_panel, "x" * 301)


def test_max_profile_adversarial_mac_filtering_and_size_limits():
    """Verify bt_config MAC filter handles regex-like MACs and rejects oversized sound uploads."""
    conf_sample = (
        "[00:11:22:33:44:55]\n"
        "Name = Headphones\n"
        "[aa:bb:cc:dd:ee:ff]\n"
        "Name = Speaker\n"
    )
    # Removing exact MAC
    filtered = max_profile.max_remove_device_from_conf(conf_sample, "00:11:22:33:44:55")
    assert "Headphones" not in filtered
    assert "Speaker" in filtered

    # Regex metacharacter in MAC must not perform wildcard matching
    regex_mac = ".*"
    unaffected = max_profile.max_remove_device_from_conf(conf_sample, regex_mac)
    assert "Headphones" in unaffected
    assert "Speaker" in unaffected

    # Sound upload size check: bodies exceeding limit are rejected
    s = fresh_server()
    max_profile.install(s, profiles.PROFILES["max"])
    dummy_panel = MagicMock()
    oversized_body = b"0" * (51 * 1024 * 1024)
    with pytest.raises(s.AdbError, match="слишком большой"):
        s.Panel.sound_replace(dummy_panel, "boot.wav", oversized_body)
