"""
Unified Standard-Library Configuration Module for Yandex Station Web Panel.
Zero external dependencies: uses dataclasses, os, pathlib, json, typing, ipaddress.
Implements a 4-tier precedence model:
  1. Explicit CLI arguments / Constructor overrides
  2. Environment variables (PANEL_*, YASTATION_*, ADB_*)
  3. JSON file persistence (.panel_settings.json)
  4. Safe defaults
"""
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import os
import sys
import json
import ipaddress
import math

ROOT_DIR = Path(__file__).resolve().parent


class ConfigError(Exception):
    """Raised when configuration validation or parsing fails."""
    pass


def _cast_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "enable", "enabled"):
        return True
    if s in ("0", "false", "no", "off", "disable", "disabled", ""):
        return False
    raise ValueError(f"Cannot cast {val!r} to boolean")


def _cast_int(val: Any) -> int:
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return int(str(val).strip())


def _cast_float(val: Any) -> float:
    if isinstance(val, bool):
        raise ValueError(f"Cannot cast boolean {val!r} to float")
    if isinstance(val, (int, float)):
        res = float(val)
    else:
        res = float(str(val).strip())
    if math.isnan(res) or math.isinf(res):
        raise ValueError(f"Float value cannot be NaN or Infinity: {val!r}")
    return res


def _cast_path(val: Any) -> Path:
    if isinstance(val, Path):
        return val
    return Path(str(val).strip()).expanduser()


def _cast_tuple_str(val: Any) -> Tuple[str, ...]:
    if isinstance(val, (list, tuple)):
        return tuple(str(x).strip() for x in val if str(x).strip())
    s = str(val).strip()
    if not s:
        return tuple()
    return tuple(x.strip() for x in s.split(",") if x.strip())


def _cast_bytes(val: Any) -> bytes:
    if isinstance(val, bytes):
        return val
    return str(val).encode("utf-8")


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    https_port: int = 8766
    data_dir: Path = field(default_factory=lambda: ROOT_DIR)
    openssl_bin: Optional[str] = None
    allow_networks: Tuple[str, ...] = field(default_factory=tuple)
    gateway_secret: Optional[str] = None
    ready_file: Optional[Path] = None
    install_mode: str = "standalone"
    open_browser: bool = False
    github_repo: str = "dimap/yapanel-web"
    analytics_hosts: Tuple[str, ...] = (
        "clck.yandex.net",
        "report.appmetrica.yandex.net",
        "appmetrica.yandex.net",
        "metrica.yandex.net",
    )
    ssl_cert_file: Optional[str] = None

    @property
    def auth_file(self) -> Path:
        return self.data_dir / ".yandex_auth.json"

    @property
    def panel_auth_file(self) -> Path:
        return self.data_dir / ".panel_auth.json"

    @property
    def metrics_file(self) -> Path:
        return self.data_dir / ".metrics.json"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / ".tls"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / ".panel_settings.json"

    @property
    def agent_file(self) -> Path:
        return self.data_dir / ".agent.json"

    @property
    def journal_file(self) -> Path:
        return self.data_dir / ".panel_journal.jsonl"

    def resolve_paths(self) -> None:
        self.data_dir = self.data_dir.resolve()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


@dataclass
class DeviceConfig:
    serial: Optional[str] = None
    adb_bin: str = "auto"
    adb_server_port: int = 5037
    use_pyadb: bool = False
    is_local: bool = False
    model: str = "auto"
    shell_timeout: float = 15.0
    alive_timeout: float = 4.0
    led_poll_interval: float = 0.4
    find_subnet: Optional[str] = None
    tv_box_serial: Optional[str] = None

    def normalized_serial(self) -> Optional[str]:
        if not self.serial:
            return None
        val = str(self.serial).strip()
        if not val or val == "local":
            return val if val else None
        if ":" not in val and not val.startswith("usb:"):
            parts = val.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                return f"{val}:5555"
            elif val.replace(".", "").isdigit():
                return f"{val}:5555"
        return val


@dataclass
class GlagolConfig:
    port: int = 1961
    ping_interval: float = 20.0
    ws_timeout: float = 10.0
    sock_timeout: float = 30.0
    cmd_timeout: float = 6.0
    qr_timeout: float = 300.0
    passport_client_id: str = "c0ebe342af7d48fbbbfcf2d2eedb8f9e"
    passport_client_secret: str = "ad0a908f0aa341a182a37ecd75bc319e"
    music_client_id: str = "23cabbbdc6cd418abb4b39c32c41195d"
    music_client_secret: str = "53bc75238f0c4d08a118e51fe9203300"
    lyrics_key: bytes = b"p93jhgh689SBReK6ghtw62"
    music_api_base: str = "https://api.music.yandex.net"
    lrclib_api_base: str = "https://lrclib.net/api"
    oauth_token_url: str = "https://oauth.mobile.yandex.net/1/token"
    login_info_url: str = "https://login.yandex.ru/info?format=json"
    quasar_token_url: str = "https://quasar.yandex.net/glagol/token"
    passport_url: str = "https://passport.yandex.ru"
    passport_base_url: str = "https://passport.yandex.ru/pwl-yandex"
    token_by_session_url: str = "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid"
    likes_cache_ttl: float = 60.0


@dataclass
class SecurityConfig:
    cookie_name: str = "yl2_session"
    session_ttl_seconds: int = 7776000  # 90 days (90 * 24 * 3600)
    pbkdf2_iter: int = 240000
    min_password_len: int = 4
    login_throttle_delay: float = 0.7
    rate_limit_max_pause: int = 900
    allowed_subnets: Tuple[str, ...] = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    route_probe_ip: str = "192.0.2.1"
    ca_validity_days: int = 3650
    cert_validity_days: int = 397

    @property
    def ca_days(self) -> int:
        return self.ca_validity_days

    @ca_days.setter
    def ca_days(self, val: int):
        self.ca_validity_days = val

    @property
    def cert_days(self) -> int:
        return self.cert_validity_days

    @cert_days.setter
    def cert_days(self, val: int):
        self.cert_validity_days = val


@dataclass
class RemotePathsConfig:
    web_dir: str = "/data/quasar/webpanel"
    quasar_dir: str = "/system/vendor/quasar"
    yam_dir: str = "/data/quazar/yam"
    yam2_root: str = "/data/yam"
    yam_port: int = 8099
    yam_registry_url: str = "https://raw.githubusercontent.com/nyakokitsu/yapkg/registry/index.json"
    yam2_registry_url: str = "https://repo.yse.wtf/v2"
    agent_port: int = 7861
    wake_port: int = 8780
    alarm_dir: str = "/data/quasar/webpanel/alarm"
    assist_dir: str = "/data/quasar/webpanel/assistant"
    viz_dir: str = "/data/quasar/webpanel/viz"
    btout_dir: str = "/data/quasar/webpanel/btout"
    wake_data_dir: str = "/data/wakeword"
    rootkeeper_path: str = "/data/quasar/rootkeeper.sh"
    initd_hook_path: str = "/etc/init.d/S99zwebpanel"
    online_check_ips: Tuple[str, ...] = ("77.88.8.8", "8.8.8.8")
    online_check_url: str = "http://ya.ru"
    analytics_hosts: Tuple[str, ...] = (
        "clck.yandex.net",
        "report.appmetrica.yandex.net",
        "appmetrica.yandex.net",
        "metrica.yandex.net",
    )
    sysfs_leds: str = "/sys/class/leds"
    tmp_dir: str = "/tmp"
    tmp_clock_dir: str = "/tmp/webpanel_clock/"
    max_ring_dev: str = "/dev/graphics/fb5"
    max_screen_dev: str = "/dev/graphics/fb4"

    @property
    def assist_bin(self) -> str:
        return f"{self.assist_dir}/assistantd"

    @property
    def assist_conf(self) -> str:
        return f"{self.assist_dir}/config.json"

    @property
    def viz_bin(self) -> str:
        return f"{self.viz_dir}/vizd"


@dataclass
class PartyConfig:
    bulbs_file: str = "bulbs.json"
    strip_file: str = "strip_config.json"
    settings_file: str = "party_settings.json"
    yandex_iot_api_base: str = "https://api.iot.yandex.net/v1.0"
    socket_timeout: float = 0.25
    socket_retry: int = 1
    socket_delay: float = 0.05
    auto_start: bool = False

    @property
    def tuya_socket_timeout(self) -> float:
        return self.socket_timeout

    @tuya_socket_timeout.setter
    def tuya_socket_timeout(self, val: float):
        self.socket_timeout = val


@dataclass
class AssistantConfig:
    default_prompt: str = (
        "Ты эрудированный собеседник и отвечаешь на любые вопросы. Отвечай по-русски и коротко: "
        "одно-три предложения разговорной речью. Ответ прочитают вслух, поэтому без списков, разметки, эмодзи, ссылок и формул."
    )
    google_tts_url: str = "https://translate.google.com/translate_tts"
    max_tokens: int = 500
    asset_name: str = "assistantd-linux-arm64"
    say_max_len: int = 300
    ask_max_len: int = 1000
    alice_cmd_limit: int = 1024
    radio_browser_api_url: str = "https://all.api.radio-browser.info/json/stations/search"


_ENV_MAP: List[Tuple[str, str, str, Callable[[Any], Any]]] = [
    # ServerConfig
    ("PANEL_HOST", "server", "host", str),
    ("PANEL_PORT", "server", "port", _cast_int),
    ("PANEL_HTTPS_PORT", "server", "https_port", _cast_int),
    ("PANEL_DATA_DIR", "server", "data_dir", _cast_path),
    ("PANEL_OPENSSL", "server", "openssl_bin", str),
    ("PANEL_ALLOW_NETWORKS", "server", "allow_networks", _cast_tuple_str),
    ("PANEL_GATEWAY_SECRET", "server", "gateway_secret", str),
    ("PANEL_READY_FILE", "server", "ready_file", _cast_path),
    ("PANEL_INSTALL", "server", "install_mode", str),
    ("PANEL_OPEN_BROWSER", "server", "open_browser", _cast_bool),
    ("PANEL_GITHUB_REPO", "server", "github_repo", str),
    ("PANEL_ANALYTICS_HOSTS", "server", "analytics_hosts", _cast_tuple_str),
    ("ANALYTICS_BLOCK_HOSTS", "server", "analytics_hosts", _cast_tuple_str),
    ("SSL_CERT_FILE", "server", "ssl_cert_file", str),

    # DeviceConfig
    ("ADB_SERIAL", "device", "serial", str),
    ("ADB", "device", "adb_bin", str),
    ("ANDROID_ADB_SERVER_PORT", "device", "adb_server_port", _cast_int),
    ("PANEL_PYADB", "device", "use_pyadb", _cast_bool),
    ("PANEL_LOCAL", "device", "is_local", _cast_bool),
    ("PANEL_MODEL", "device", "model", str),
    ("PANEL_ADB_TIMEOUT", "device", "shell_timeout", _cast_float),
    ("PANEL_ADB_ALIVE_TIMEOUT", "device", "alive_timeout", _cast_float),
    ("PANEL_LED_POLL_INTERVAL", "device", "led_poll_interval", _cast_float),
    ("PANEL_TV_BOX_SERIAL", "device", "tv_box_serial", str),

    # GlagolConfig
    ("YASTATION_GLAGOL_PORT", "glagol", "port", _cast_int),
    ("YASTATION_GLAGOL_PING_INTERVAL", "glagol", "ping_interval", _cast_float),
    ("YASTATION_GLAGOL_WS_TIMEOUT", "glagol", "ws_timeout", _cast_float),
    ("YASTATION_GLAGOL_SOCK_TIMEOUT", "glagol", "sock_timeout", _cast_float),
    ("YASTATION_GLAGOL_CMD_TIMEOUT", "glagol", "cmd_timeout", _cast_float),
    ("YASTATION_GLAGOL_QR_TIMEOUT", "glagol", "qr_timeout", _cast_float),
    ("YASTATION_PASSPORT_CLIENT_ID", "glagol", "passport_client_id", str),
    ("YASTATION_PASSPORT_CLIENT_SECRET", "glagol", "passport_client_secret", str),
    ("YASTATION_MUSIC_CLIENT_ID", "glagol", "music_client_id", str),
    ("YASTATION_MUSIC_CLIENT_SECRET", "glagol", "music_client_secret", str),
    ("YASTATION_LYRICS_KEY", "glagol", "lyrics_key", _cast_bytes),
    ("YASTATION_MUSIC_API_BASE", "glagol", "music_api_base", str),
    ("YASTATION_LRCLIB_API_BASE", "glagol", "lrclib_api_base", str),
    ("YASTATION_OAUTH_TOKEN_URL", "glagol", "oauth_token_url", str),
    ("YASTATION_LOGIN_INFO_URL", "glagol", "login_info_url", str),
    ("YASTATION_QUASAR_TOKEN_URL", "glagol", "quasar_token_url", str),

    # SecurityConfig
    ("PANEL_COOKIE_NAME", "security", "cookie_name", str),
    ("PANEL_SESSION_COOKIE", "security", "cookie_name", str),
    ("PANEL_SESSION_TTL", "security", "session_ttl_seconds", _cast_int),
    ("PANEL_PBKDF2_ITER", "security", "pbkdf2_iter", _cast_int),
    ("PANEL_MIN_PASSWORD", "security", "min_password_len", _cast_int),
    ("PANEL_LOGIN_THROTTLE", "security", "login_throttle_delay", _cast_float),
    ("PANEL_RATE_LIMIT_MAX_PAUSE", "security", "rate_limit_max_pause", _cast_int),
    ("PANEL_ALLOWED_SUBNETS", "security", "allowed_subnets", _cast_tuple_str),
    ("PANEL_ROUTE_PROBE_IP", "security", "route_probe_ip", str),
    ("YASTATION_ROUTE_PROBE_IP", "security", "route_probe_ip", str),
    ("PANEL_TLS_CA_DAYS", "security", "ca_validity_days", _cast_int),
    ("TLS_CA_DAYS", "security", "ca_validity_days", _cast_int),
    ("PANEL_TLS_CERT_DAYS", "security", "cert_validity_days", _cast_int),
    ("TLS_CERT_DAYS", "security", "cert_validity_days", _cast_int),

    # RemotePathsConfig
    ("YASTATION_REMOTE_WEB_DIR", "remote", "web_dir", str),
    ("YASTATION_QUASAR_DIR", "remote", "quasar_dir", str),
    ("YASTATION_YAM_DIR", "remote", "yam_dir", str),
    ("YASTATION_YAM2_ROOT", "remote", "yam2_root", str),
    ("YASTATION_AGENT_PORT", "remote", "agent_port", _cast_int),
    ("YASTATION_WAKE_PORT", "remote", "wake_port", _cast_int),
    ("YASTATION_YAM_PORT", "remote", "yam_port", _cast_int),
    ("YASTATION_YAM_REGISTRY", "remote", "yam_registry_url", str),
    ("YASTATION_YAM2_REGISTRY", "remote", "yam2_registry_url", str),
    ("YASTATION_REMOTE_ALARM_DIR", "remote", "alarm_dir", str),
    ("YASTATION_REMOTE_ASSIST_DIR", "remote", "assist_dir", str),
    ("YASTATION_REMOTE_VIZ_DIR", "remote", "viz_dir", str),
    ("YASTATION_REMOTE_BTOUT_DIR", "remote", "btout_dir", str),
    ("YASTATION_REMOTE_WAKE_DIR", "remote", "wake_data_dir", str),
    ("YASTATION_ROOTKEEPER_PATH", "remote", "rootkeeper_path", str),
    ("YASTATION_INITD_HOOK_PATH", "remote", "initd_hook_path", str),
    ("ONLINE_CHECK_IPS", "remote", "online_check_ips", _cast_tuple_str),
    ("ONLINE_CHECK_URL", "remote", "online_check_url", str),
    ("YASTATION_TMP_CLOCK_DIR", "remote", "tmp_clock_dir", str),
    ("STATION_MAX_FB_RING", "remote", "max_ring_dev", str),
    ("STATION_MAX_FB_SCREEN", "remote", "max_screen_dev", str),

    # PartyConfig
    ("PARTY_BULBS_FILE", "party", "bulbs_file", str),
    ("PARTY_STRIP_FILE", "party", "strip_file", str),
    ("PARTY_SETTINGS_FILE", "party", "settings_file", str),
    ("YANDEX_IOT_API_BASE", "party", "yandex_iot_api_base", str),
    ("TUYA_SOCKET_TIMEOUT", "party", "socket_timeout", _cast_float),
    ("TUYA_SOCKET_RETRY", "party", "socket_retry", _cast_int),
    ("TUYA_SOCKET_DELAY", "party", "socket_delay", _cast_float),
    ("PARTY_AUTO_START", "party", "auto_start", _cast_bool),

    # AssistantConfig
    ("GOOGLE_TTS_URL", "assistant", "google_tts_url", str),
    ("ASSISTANT_MAX_TOKENS", "assistant", "max_tokens", _cast_int),
    ("RADIO_BROWSER_API_URL", "assistant", "radio_browser_api_url", str),
]


@dataclass
class PanelConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    glagol: GlagolConfig = field(default_factory=GlagolConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    remote: RemotePathsConfig = field(default_factory=RemotePathsConfig)
    party: PartyConfig = field(default_factory=PartyConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)

    def resolve_paths(self) -> None:
        self.server.resolve_paths()

    def to_dict(self) -> Dict[str, Any]:
        """Convert entire config tree to JSON-serializable dictionary."""
        d = asdict(self)
        d["server"]["data_dir"] = str(self.server.data_dir)
        d["server"]["ready_file"] = str(self.server.ready_file) if self.server.ready_file else None
        d["glagol"]["lyrics_key"] = self.glagol.lyrics_key.decode("utf-8", "replace")
        return d

    def _apply_env(self, environ: Dict[str, str], strict: bool = False) -> None:
        for env_key, section, field_name, cast_fn in _ENV_MAP:
            if env_key in environ:
                raw_val = environ[env_key]
                sec_obj = getattr(self, section)
                try:
                    casted = cast_fn(raw_val)
                    setattr(sec_obj, field_name, casted)
                except Exception as e:
                    if strict:
                        raise ConfigError(f"Failed to cast env {env_key}={raw_val!r}: {e}") from e

    def _apply_cli(self, cli_args: Any, strict: bool = False) -> None:
        if hasattr(cli_args, "__dict__"):
            cli_dict = vars(cli_args)
        elif isinstance(cli_args, dict):
            cli_dict = cli_args
        else:
            return

        cli_mappings: Dict[str, Tuple[str, str, Callable[[Any], Any]]] = {
            "host": ("server", "host", str),
            "port": ("server", "port", _cast_int),
            "https_port": ("server", "https_port", _cast_int),
            "data_dir": ("server", "data_dir", _cast_path),
            "device": ("device", "serial", str),
            "serial": ("device", "serial", str),
            "model": ("device", "model", str),
            "find": ("device", "find_subnet", str),
            "find_subnet": ("device", "find_subnet", str),
            "open": ("server", "open_browser", _cast_bool),
            "open_browser": ("server", "open_browser", _cast_bool),
        }

        for cli_key, (section, field_name, cast_fn) in cli_mappings.items():
            if cli_key in cli_dict:
                raw_val = cli_dict[cli_key]
                if raw_val is not None:
                    # Ignore default False for --open if not explicitly passed in sys.argv
                    if cli_key in ("open", "open_browser") and raw_val is False:
                        if not any(arg in sys.argv for arg in ("--open", "--open_browser", "--open-browser")):
                            continue
                    sec_obj = getattr(self, section)
                    try:
                        casted = cast_fn(raw_val)
                        setattr(sec_obj, field_name, casted)
                    except Exception as e:
                        if strict:
                            raise ConfigError(f"Failed to apply CLI arg {cli_key}={raw_val!r}: {e}") from e

    @classmethod
    def load(
        cls,
        settings_path: Optional[Union[str, Path]] = None,
        env: Optional[Dict[str, str]] = None,
        cli_args: Optional[Any] = None,
        strict: bool = False,
    ) -> "PanelConfig":
        """
        Loads configuration applying the 4-tier precedence:
        CLI > ENV > JSON > Defaults.
        """
        cfg = cls()
        environ = os.environ if env is None else env

        # Resolve settings JSON file path and target data_dir
        if settings_path:
            sp = Path(settings_path)
            if sp.is_dir() or sp.suffix != ".json":
                json_path = sp / ".panel_settings.json"
                cfg.server.data_dir = sp
            else:
                json_path = sp
                cfg.server.data_dir = sp.parent
        else:
            env_data_dir = environ.get("PANEL_DATA_DIR")
            if env_data_dir:
                cfg.server.data_dir = Path(env_data_dir)
            json_path = cfg.server.data_dir / ".panel_settings.json"

        # Tier 3: JSON File loading
        if json_path.is_file():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    # Flat legacy keys
                    if "device" in data and isinstance(data["device"], str):
                        cfg.device.serial = data["device"]
                    if "model" in data and isinstance(data["model"], str):
                        cfg.device.model = data["model"]
                    # Structured sections
                    for sec_name in ("server", "device", "glagol", "security", "remote", "party", "assistant"):
                        if sec_name in data and isinstance(data[sec_name], dict):
                            sec_obj = getattr(cfg, sec_name)
                            for k, v in data[sec_name].items():
                                if hasattr(sec_obj, k):
                                    cur_val = getattr(sec_obj, k)
                                    try:
                                        if k == "ready_file" and v is not None:
                                            v = _cast_path(v)
                                        elif isinstance(cur_val, bool):
                                            v = _cast_bool(v)
                                        elif isinstance(cur_val, int) and not isinstance(cur_val, bool):
                                            v = _cast_int(v)
                                        elif isinstance(cur_val, float):
                                            v = _cast_float(v)
                                        elif isinstance(cur_val, Path):
                                            v = _cast_path(v)
                                        elif isinstance(cur_val, tuple):
                                            if isinstance(v, (list, tuple)):
                                                v = tuple(v)
                                            else:
                                                raise TypeError(f"Expected list or tuple for {sec_name}.{k}, got {type(v).__name__}")
                                        elif isinstance(cur_val, bytes):
                                            if isinstance(v, str):
                                                v = v.encode("utf-8")
                                            elif isinstance(v, (bytes, bytearray)):
                                                v = bytes(v)
                                            else:
                                                raise TypeError(f"Expected str or bytes for {sec_name}.{k}, got {type(v).__name__}")
                                        elif isinstance(cur_val, str):
                                            if isinstance(v, (dict, list, tuple, set)):
                                                raise TypeError(f"Expected str for {sec_name}.{k}, got {type(v).__name__}")
                                            elif not isinstance(v, str) and v is not None:
                                                v = str(v)
                                        elif cur_val is None and k in (
                                            "serial", "find_subnet", "tv_box_serial",
                                            "openssl_bin", "gateway_secret", "ssl_cert_file"
                                        ):
                                            if isinstance(v, (dict, list, tuple, set)):
                                                raise TypeError(f"Expected str or None for {sec_name}.{k}, got {type(v).__name__}")
                                            elif v is not None and not isinstance(v, str):
                                                v = str(v)
                                        setattr(sec_obj, k, v)
                                    except (ValueError, TypeError) as e:
                                        if strict:
                                            raise ConfigError(f"Failed to cast settings JSON field {sec_name}.{k}={v!r}: {e}") from e
                                        continue
            except ConfigError:
                raise
            except Exception as e:
                if strict:
                    raise ConfigError(f"Failed to parse settings JSON {json_path}: {e}") from e

        # Tier 2: Environment Variables
        cfg._apply_env(environ, strict=strict)

        # Tier 1: CLI Overrides
        if cli_args is not None:
            cfg._apply_cli(cli_args, strict=strict)

        # Final path resolution
        cfg.resolve_paths()
        return cfg


_GLOBAL_CONFIG: Optional[PanelConfig] = None


def get_config() -> PanelConfig:
    """Retrieve global cached configuration or load default."""
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None:
        _GLOBAL_CONFIG = PanelConfig.load()
    return _GLOBAL_CONFIG


def set_config(cfg: PanelConfig) -> None:
    """Set global configuration singleton."""
    global _GLOBAL_CONFIG
    _GLOBAL_CONFIG = cfg
