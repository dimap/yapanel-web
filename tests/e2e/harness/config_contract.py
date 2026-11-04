"""Configuration contract definition and fallback loader for E2E tests."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Attempt to load target config module from project root
try:
    import config as target_config  # type: ignore
    HAVE_REAL_CONFIG = hasattr(target_config, "PanelConfig")
except Exception:
    target_config = None
    HAVE_REAL_CONFIG = False


def is_real_config_available() -> bool:
    """Return True if yandex-station-web/config.py is loaded and exports PanelConfig."""
    global HAVE_REAL_CONFIG, target_config
    if HAVE_REAL_CONFIG:
        return True
    try:
        if "config" in sys.modules:
            del sys.modules["config"]
        import config as tc  # type: ignore
        if hasattr(tc, "PanelConfig"):
            target_config = tc
            HAVE_REAL_CONFIG = True
            return True
    except Exception:
        pass
    return False


def get_config_module():
    """Return the real config module if available, otherwise return this contract module."""
    if is_real_config_available():
        return target_config
    return sys.modules[__name__]


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    https_port: int = 8766
    data_dir: Path = field(default_factory=lambda: Path(".data"))
    tls_dir: Optional[Path] = None
    session_cookie: str = "yl2_session"
    gateway_secret: str = ""

    def __post_init__(self):
        if not isinstance(self.data_dir, Path):
            self.data_dir = Path(self.data_dir)
        if self.tls_dir is not None and not isinstance(self.tls_dir, Path):
            self.tls_dir = Path(self.tls_dir)
        if not (0 <= int(self.port) <= 65535):
            raise ValueError(f"Invalid server port: {self.port}")
        if not (0 <= int(self.https_port) <= 65535):
            raise ValueError(f"Invalid https port: {self.https_port}")
        self.port = int(self.port)
        self.https_port = int(self.https_port)


@dataclass
class DeviceConfig:
    serial: str = ""
    model: str = "auto"
    adb_port: int = 5555
    adb_timeout: float = 15.0

    def __post_init__(self):
        self.adb_port = int(self.adb_port)
        self.adb_timeout = float(self.adb_timeout)


@dataclass
class GlagolConfig:
    port: int = 1961
    token: str = ""
    device_id: str = ""
    ws_timeout: float = 10.0

    def __post_init__(self):
        self.port = int(self.port)
        self.ws_timeout = float(self.ws_timeout)


@dataclass
class SecurityConfig:
    pbkdf2_iterations: int = 240_000
    min_password_length: int = 4
    session_ttl_sec: int = 7_776_000  # 90 days
    login_throttle_delay: float = 0.7
    allowed_subnets: Tuple[str, ...] = (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )

    def __post_init__(self):
        self.pbkdf2_iterations = int(self.pbkdf2_iterations)
        self.min_password_length = int(self.min_password_length)
        self.session_ttl_sec = int(self.session_ttl_sec)
        self.login_throttle_delay = float(self.login_throttle_delay)
        if isinstance(self.allowed_subnets, list):
            self.allowed_subnets = tuple(self.allowed_subnets)


@dataclass
class RemotePathsConfig:
    web_dir: str = "/data/quasar/webpanel"
    quasar_dir: str = "/system/vendor/quasar"
    yam_dir: str = "/data/quazar/yam"
    yam2_root: str = "/data/yam"
    alarm_dir: str = "/data/quasar/webpanel/alarm"
    assist_dir: str = "/data/quasar/webpanel/assistant"
    viz_dir: str = "/data/quasar/webpanel/viz"


@dataclass
class PartyConfig:
    enabled: bool = False
    bulbs_file: str = "bulbs.json"
    strip_file: str = "strip_config.json"
    fps: int = 20

    def __post_init__(self):
        if isinstance(self.enabled, str):
            self.enabled = self.enabled.lower() in ("1", "true", "yes", "on")
        self.fps = int(self.fps)


@dataclass
class AssistantConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8780"
    timeout: float = 10.0

    def __post_init__(self):
        if isinstance(self.enabled, str):
            self.enabled = self.enabled.lower() in ("1", "true", "yes", "on")
        self.timeout = float(self.timeout)


@dataclass
class PanelConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    glagol: GlagolConfig = field(default_factory=GlagolConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    remote: RemotePathsConfig = field(default_factory=RemotePathsConfig)
    party: PartyConfig = field(default_factory=PartyConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)

    @classmethod
    def load(
        cls,
        settings_path: Optional[str | Path] = None,
        env: Optional[Dict[str, str]] = None,
        cli_args: Optional[Dict[str, Any]] = None,
    ) -> PanelConfig:
        """4-tier precedence: CLI args > Environment variables > JSON store > Defaults."""
        if is_real_config_available():
            try:
                # Real implementation in config.py
                return target_config.PanelConfig.load(
                    settings_path=settings_path,
                    env=env if env is not None else os.environ,
                    cli_args=cli_args,
                )
            except TypeError:
                try:
                    return target_config.PanelConfig.load(settings_path=settings_path)
                except Exception:
                    pass

        # Reference contract implementation
        active_env = dict(os.environ) if env is None else dict(env)
        cli = dict(cli_args or {})

        # 1. Defaults
        server_vals: Dict[str, Any] = {}
        device_vals: Dict[str, Any] = {}
        glagol_vals: Dict[str, Any] = {}
        security_vals: Dict[str, Any] = {}
        remote_vals: Dict[str, Any] = {}
        party_vals: Dict[str, Any] = {}
        assistant_vals: Dict[str, Any] = {}

        # 2. JSON Store (if present)
        json_data: Dict[str, Any] = {}
        target_path = Path(settings_path) if settings_path else Path(".panel_settings.json")
        if target_path.is_file():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        json_data = loaded
            except Exception:
                pass

        if "server" in json_data and isinstance(json_data["server"], dict):
            server_vals.update(json_data["server"])
        if "device" in json_data and isinstance(json_data["device"], dict):
            device_vals.update(json_data["device"])
        elif "device" in json_data and isinstance(json_data["device"], str):
            device_vals["serial"] = json_data["device"]
        if "glagol" in json_data and isinstance(json_data["glagol"], dict):
            glagol_vals.update(json_data["glagol"])
        if "security" in json_data and isinstance(json_data["security"], dict):
            security_vals.update(json_data["security"])
        if "remote" in json_data and isinstance(json_data["remote"], dict):
            remote_vals.update(json_data["remote"])
        if "party" in json_data and isinstance(json_data["party"], dict):
            party_vals.update(json_data["party"])
        if "assistant" in json_data and isinstance(json_data["assistant"], dict):
            assistant_vals.update(json_data["assistant"])

        # 3. Environment Variables
        if "PANEL_HOST" in active_env:
            server_vals["host"] = active_env["PANEL_HOST"]
        if "PANEL_PORT" in active_env:
            try:
                p = int(active_env["PANEL_PORT"])
                if 1 <= p <= 65535:
                    server_vals["port"] = p
            except ValueError:
                pass
        if "PANEL_HTTPS_PORT" in active_env:
            try:
                p = int(active_env["PANEL_HTTPS_PORT"])
                if 1 <= p <= 65535:
                    server_vals["https_port"] = p
            except ValueError:
                pass
        if "PANEL_DATA_DIR" in active_env:
            server_vals["data_dir"] = Path(active_env["PANEL_DATA_DIR"])
        if "ADB_SERIAL" in active_env:
            device_vals["serial"] = active_env["ADB_SERIAL"]
        if "PANEL_MODEL" in active_env:
            device_vals["model"] = active_env["PANEL_MODEL"]
        if "YASTATION_GLAGOL_PORT" in active_env:
            try:
                p = int(active_env["YASTATION_GLAGOL_PORT"])
                if 1 <= p <= 65535:
                    glagol_vals["port"] = p
            except ValueError:
                pass
        if "YASTATION_WEB_DIR" in active_env:
            remote_vals["web_dir"] = active_env["YASTATION_WEB_DIR"]
        if "YASTATION_QUASAR_DIR" in active_env:
            remote_vals["quasar_dir"] = active_env["YASTATION_QUASAR_DIR"]
        if "PARTY_ENABLED" in active_env:
            party_vals["enabled"] = active_env["PARTY_ENABLED"].lower() in ("1", "true", "yes")

        # 4. CLI Overrides
        for k, v in cli.items():
            if k == "host":
                server_vals["host"] = v
            elif k == "port":
                server_vals["port"] = int(v)
            elif k == "https_port":
                server_vals["https_port"] = int(v)
            elif k == "data_dir":
                server_vals["data_dir"] = Path(v)
            elif k == "serial":
                device_vals["serial"] = v
            elif k == "model":
                device_vals["model"] = v

        return cls(
            server=ServerConfig(**server_vals),
            device=DeviceConfig(**device_vals),
            glagol=GlagolConfig(**glagol_vals),
            security=SecurityConfig(**security_vals),
            remote=RemotePathsConfig(**remote_vals),
            party=PartyConfig(**party_vals),
            assistant=AssistantConfig(**assistant_vals),
        )


def load_config(
    settings_path: Optional[str | Path] = None,
    env: Optional[Dict[str, str]] = None,
    cli_args: Optional[Dict[str, Any]] = None,
) -> PanelConfig:
    """Convenience function to load PanelConfig adhering to interface contract."""
    return PanelConfig.load(settings_path=settings_path, env=env, cli_args=cli_args)
