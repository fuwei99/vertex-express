import json
import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

load_dotenv()


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT_DIR / "config.json"

_KEY_ALIASES = {
    "API_KEY": ("api_key",),
    "ADMIN_PASSWORD": ("admin_password",),
    "VERTEX_EXPRESS_API_KEY": ("vertex_express_api_key", "vertex_express_api_keys"),
    "SUBSCRIPTION_URL": ("subscription_url",),
    "PROXY_URL": ("proxy_url",),
    "GOOGLE_CREDENTIALS_JSON": ("google_credentials_json",),
    "CREDENTIALS_DIR": ("credentials_dir",),
    "HUGGINGFACE": ("huggingface",),
    "HUGGINGFACE_API_KEY": ("huggingface_api_key",),
    "FAKE_STREAMING": ("fake_streaming",),
    "FAKE_STREAMING_INTERVAL": ("fake_streaming_interval",),
    "MODELS_CONFIG_URL": ("models_config_url",),
    "ROUNDROBIN": ("roundrobin",),
    "SAFETY_SCORE": ("safety_score",),
    "SSL_CERT_FILE": ("ssl_cert_file",),
    "MAX_RETRIES_429": ("max_retries_429",),
    "RETRIES_BEFORE_SWITCH": ("retries_before_switch",),
    "ANTI429_ASSIST": ("anti429_enabled", "anti429_assist"),
    "PROXY_ROUTE_ENABLED": ("proxy_route_enabled", "anti_tracking"),
    "DROP_MAX_TOKENS": ("drop_max_tokens",),
    "VERTEX_LOCATION": ("vertex_location", "location"),
    "AUTO_VERTEX_LOCATION": ("auto_vertex_location",),
    "LOG_TIMEZONE": ("log_timezone",),
    "PROJECT_ID_MAP": ("project_id_map",),
    "PORT": ("port_api", "port"),
}


def load_config_json() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        # Try to restore from CONFIG environment variable
        raw_config = os.environ.get("CONFIG", "").strip()
        if raw_config:
            # Strip potential quotes if they were added by shell/dotenv
            if (raw_config.startswith("'") and raw_config.endswith("'")) or \
               (raw_config.startswith('"') and raw_config.endswith('"')):
                raw_config = raw_config[1:-1].strip()
            try:
                data = json.loads(raw_config)
                if isinstance(data, dict):
                    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    print(f"INFO: Generated {CONFIG_FILE} from CONFIG environment variable.")
                    return data
            except Exception as exc:
                print(f"WARNING: Failed to parse CONFIG environment variable: {exc}")
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"WARNING: Failed to read config.json: {exc}")
        return {}


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _lookup(data: dict[str, Any], env_key: str) -> Any:
    if env_key in data:
        return data[env_key]
    for alias in _KEY_ALIASES.get(env_key, ()):
        if alias in data:
            return data[alias]
    return None


def apply_config_json_to_env(override: bool = True) -> dict[str, Any]:
    data = load_config_json()
    for env_key in _KEY_ALIASES:
        value = _lookup(data, env_key)
        if value is None:
            continue
        # If override is True, always set it. Otherwise only if not already set.
        if override or not os.environ.get(env_key):
            os.environ[env_key] = _normalize_value(value)
    return data


def get_config_value(env_key: str, default: str = "") -> str:
    # 1. First priority: config.json
    value = _lookup(load_config_json(), env_key)
    if value is not None:
        return _normalize_value(value).strip()
    
    # 2. Second priority: environment variables (including .env)
    raw_env = os.environ.get(env_key, "").strip()
    if raw_env:
        return raw_env
        
    # 3. Third priority: default value
    return default


def write_config_values(updates: dict[str, Any]) -> None:
    data = load_config_json()
    for env_key, raw_value in updates.items():
        aliases = _KEY_ALIASES.get(env_key, ())
        key = aliases[0] if aliases else env_key.lower()
        if env_key == "VERTEX_EXPRESS_API_KEY" and isinstance(raw_value, str):
            data[key] = [part.strip() for part in raw_value.split(",") if part.strip()]
        else:
            data[key] = raw_value
        os.environ[env_key] = _normalize_value(raw_value)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
