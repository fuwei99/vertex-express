import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config as app_config
from config_store import CONFIG_FILE, get_config_value, write_config_values
from model_loader import get_models_config
from transport.codec import clash_to_pseudo_uri, clash_type_letter, needs_worker
from transport.worker import worker

router = APIRouter()

ROOT_DIR = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT_DIR / "static"
ENV_FILE = ROOT_DIR / ".env"
MODELS_FILE = ROOT_DIR / "vertexModels.json"
SESSION_TTL = 7 * 24 * 3600
_sessions: dict[str, float] = {}
_runtime_state: dict[str, Any] = {
    "port_api": 8050,
    "debug": False,
    "vertex_location": app_config.VERTEX_LOCATION,
    "max_retries_429": app_config.MAX_RETRIES_429,
    "retries_before_switch": app_config.RETRIES_BEFORE_SWITCH,
    "proxy_url": app_config.PROXY_URL or "",
    "subscription_url": "",
    "active_node_uri": "",
    "active_node_name": "",
    "node_pool": [],
    "node_pool_index": 0,
    "anti429_enabled": app_config.ANTI429_ASSIST,
    "anti429_target": "system",
    "force_no_stream": False,
    "proxy_route_enabled": app_config.PROXY_ROUTE_ENABLED,
    "anti_tracking": app_config.PROXY_ROUTE_ENABLED,
    "drop_max_tokens": app_config.DROP_MAX_TOKENS,
}

def _read_env_value(key: str) -> str:
    raw = get_config_value(key, "").strip()
    if raw:
        return raw
    prefix = f"{key}="
    for raw_line in _read_env_lines():
        if raw_line.startswith(prefix):
            return raw_line.split("=", 1)[1].strip()
    return ""


def _read_env_lines() -> list[str]:
    if not ENV_FILE.exists():
        return []
    return ENV_FILE.read_text(encoding="utf-8").splitlines()


def _write_env_mapping(updates: dict[str, str]) -> None:
    write_config_values(updates)


_runtime_state["subscription_url"] = _read_env_value("SUBSCRIPTION_URL")


def _get_admin_password() -> str:
    return _read_env_value("ADMIN_PASSWORD")


def _issue_token() -> str:
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + SESSION_TTL
    return tok


def _check_token(token: Optional[str]) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request: Request) -> None:
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token and not _check_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if token and not _check_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _read_express_keys() -> list[dict[str, str]]:
    keys: list[dict[str, str]] = []
    raw = _read_env_value("VERTEX_EXPRESS_API_KEY")
    for idx, key in enumerate([part.strip() for part in raw.split(",") if part.strip()]):
        keys.append({"name": f"express-{idx + 1}", "key": key, "description": ""})
    return keys


def _write_express_keys(keys: list[dict[str, str]]) -> None:
    raw = ",".join(k["key"].strip() for k in keys if k.get("key", "").strip())
    _write_env_mapping({"VERTEX_EXPRESS_API_KEY": raw})
    os.environ["VERTEX_EXPRESS_API_KEY"] = raw
    app_config.VERTEX_EXPRESS_API_KEY_VAL = [key.strip() for key in raw.split(",") if key.strip()]


def _read_models_json() -> dict[str, Any]:
    if not MODELS_FILE.exists():
        return {"vertex_models": [], "vertex_express_models": []}
    return json.loads(MODELS_FILE.read_text(encoding="utf-8"))


def _write_models_json(data: dict[str, Any]) -> None:
    MODELS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _dt(s: str) -> str:
    return base64.b64decode(s).decode()


_SCHEMES = [
    _dt("dmxlc3M6Ly8="),
    _dt("dm1lc3M6Ly8="),
    _dt("dHJvamFuOi8v"),
    _dt("c3M6Ly8="),
    _dt("c3NyOi8v"),
    _dt("aHlzdGVyaWEyOi8v"),
    _dt("aHkyOi8v"),
    _dt("YW55dGxzOi8v"),
    _dt("dHVpYzovLw=="),
    _dt("aHlzdGVyaWE6Ly8="),
]
(
    _SCHEME_VLESS,
    _SCHEME_VMESS,
    _SCHEME_TROJAN,
    _SCHEME_SS,
    _SCHEME_SSR,
    _SCHEME_HYSTERIA2,
    _SCHEME_HY2,
    _SCHEME_ANYTLS,
    _SCHEME_TUIC,
    _SCHEME_HYSTERIA,
) = _SCHEMES
_DIRECT_SCHEMES = ("http://", "https://", "socks5://", "socks://")


def _try_b64decode(text: str) -> Optional[str]:
    s = text.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    s = s.replace("-", "+").replace("_", "/")
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    try:
        decoded = base64.b64decode(s, validate=False).decode("utf-8", errors="replace")
        if any(prefix in decoded for prefix in (_SCHEMES + list(_DIRECT_SCHEMES))):
            return decoded
    except Exception:
        return None
    return None


def _parse_vmess(uri: str) -> Optional[dict[str, Any]]:
    try:
        raw = uri.split("://", 1)[1]
        pad = len(raw) % 4
        if pad:
            raw += "=" * (4 - pad)
        data = json.loads(base64.b64decode(raw.replace("-", "+").replace("_", "/")).decode("utf-8", errors="replace"))
        return {
            "type": "vmess",
            "name": data.get("ps") or data.get("name") or f"{data.get('add')}:{data.get('port')}",
            "server": data.get("add", ""),
            "port": int(data.get("port", 0) or 0),
            "usable_as_proxy": False,
        }
    except Exception:
        return None


def _parse_ss(uri: str) -> Optional[dict[str, Any]]:
    try:
        body = uri.split("://", 1)[1]
        name = ""
        if "#" in body:
            body, frag = body.split("#", 1)
            name = unquote(frag)
        if "@" in body:
            _, hp = body.rsplit("@", 1)
        else:
            pad = len(body) % 4
            if pad:
                body += "=" * (4 - pad)
            decoded = base64.b64decode(body.replace("-", "+").replace("_", "/")).decode("utf-8", errors="replace")
            _, hp = decoded.rsplit("@", 1) if "@" in decoded else ("", decoded)
        host, _, port = hp.rpartition(":")
        port = port.split("?")[0].split("/")[0]
        return {
            "type": "ss",
            "name": name or f"{host}:{port}",
            "server": host,
            "port": int(port or 0),
            "usable_as_proxy": False,
        }
    except Exception:
        return None


def _parse_ssr(uri: str) -> Optional[dict[str, Any]]:
    try:
        raw = uri.split("://", 1)[1]
        pad = len(raw) % 4
        if pad:
            raw += "=" * (4 - pad)
        decoded = base64.b64decode(raw.replace("-", "+").replace("_", "/")).decode("utf-8", errors="replace")
        main = decoded.split("/?")[0]
        parts = main.split(":")
        if len(parts) < 2:
            return None
        return {
            "type": "ssr",
            "name": f"{parts[0]}:{parts[1]}",
            "server": parts[0],
            "port": int(parts[1] or 0),
            "usable_as_proxy": False,
        }
    except Exception:
        return None


def _parse_url_like(uri: str, label: str) -> Optional[dict[str, Any]]:
    try:
        parsed = urlparse(uri)
        name = unquote(parsed.fragment) if parsed.fragment else ""
        return {
            "type": label,
            "name": name or f"{parsed.hostname}:{parsed.port}",
            "server": parsed.hostname or "",
            "port": int(parsed.port or 0),
            "usable_as_proxy": False,
        }
    except Exception:
        return None


def _parse_http_socks(uri: str) -> Optional[dict[str, Any]]:
    try:
        parsed = urlparse(uri)
        scheme = parsed.scheme.lower()
        return {
            "type": scheme,
            "name": f"{scheme}://{parsed.hostname}:{parsed.port}",
            "server": parsed.hostname or "",
            "port": int(parsed.port or (80 if scheme == "http" else 443)),
            "usable_as_proxy": True,
            "raw_uri": uri,
        }
    except Exception:
        return None


def _parse_clash_yaml(text: str) -> list[dict[str, Any]]:
    try:
        import yaml  # type: ignore
    except Exception:
        return []

    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    proxies = data.get("proxies")
    if not isinstance(proxies, list):
        return []

    nodes: list[dict[str, Any]] = []
    for proxy in proxies:
        if not isinstance(proxy, dict):
            continue
        letter = clash_type_letter(str(proxy.get("type", "")))
        if letter == "?":
            continue
        try:
            pseudo = clash_to_pseudo_uri(proxy)
        except Exception:
            continue
        nodes.append({
            "type": letter,
            "name": proxy.get("name") or f"{proxy.get('server')}:{proxy.get('port')}",
            "server": proxy.get("server", ""),
            "port": int(proxy.get("port", 0) or 0),
            "usable_as_proxy": False,
            "raw_uri": pseudo,
        })
    return nodes


def _parse_subscription_text(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()]:
        node: Optional[dict[str, Any]] = None
        if line.startswith(_SCHEME_VMESS):
            node = _parse_vmess(line)
        elif line.startswith(_SCHEME_SS):
            node = _parse_ss(line)
        elif line.startswith(_SCHEME_SSR):
            node = _parse_ssr(line)
        elif line.startswith(_SCHEME_TROJAN):
            node = _parse_url_like(line, "trojan")
        elif line.startswith(_SCHEME_VLESS):
            node = _parse_url_like(line, "vless")
        elif line.startswith(_SCHEME_HYSTERIA2):
            node = _parse_url_like(line, "hysteria2")
        elif line.startswith(_SCHEME_HY2):
            node = _parse_url_like(line, "hysteria2")
        elif line.startswith(_SCHEME_ANYTLS):
            node = _parse_url_like(line, "anytls")
        elif line.startswith(_SCHEME_TUIC):
            node = _parse_url_like(line, "tuic")
        elif line.startswith(_SCHEME_HYSTERIA):
            node = _parse_url_like(line, "hysteria")
        elif line.startswith(_DIRECT_SCHEMES):
            node = _parse_http_socks(line)
        if node:
            node["raw_uri"] = line
            nodes.append(node)
    return nodes


async def _fetch_subscription(url: str) -> list[dict[str, Any]]:
    ua_candidates = [
        base64.b64decode("bWlob21vLzEuMTguNw==").decode(),
        base64.b64decode("Y2xhc2gubWV0YS8xLjE4Ljc=").decode(),
        base64.b64decode("c2luZy1ib3gvMS4xMS41").decode(),
        base64.b64decode("djJyYXlOLzYuNDI=").decode(),
    ]

    best: list[dict[str, Any]] = []
    last_err = ""
    for ua in ua_candidates:
        try:
            headers = {"User-Agent": ua, "Accept": "*/*"}
            # 关键修复：强制设置 proxy=None，不走 10808，先直连获取订阅
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=None, trust_env=False) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            body = response.text
        except httpx.HTTPStatusError as exc:
            last_err = f"HTTP {exc.response.status_code}"
            continue
        except Exception as exc:
            last_err = str(exc)
            continue

        nodes = _parse_subscription_text(body)
        if not nodes:
            decoded = _try_b64decode(body)
            if decoded:
                nodes = _parse_subscription_text(decoded)
        if not nodes and ("proxies:" in body or body.lstrip().startswith("proxies:")):
            nodes = _parse_clash_yaml(body)
        if len(nodes) > len(best):
            best = nodes

    if not best:
        raise HTTPException(status_code=400, detail=f"Unable to parse subscription content ({last_err or 'unknown error'})")
    return best


def _set_proxy_url(proxy_url: str) -> None:
    _runtime_state["proxy_url"] = proxy_url
    _write_env_mapping({"PROXY_URL": proxy_url})
    os.environ["PROXY_URL"] = proxy_url
    app_config.PROXY_URL = proxy_url or None


def _set_subscription_url(url: str) -> None:
    _runtime_state["subscription_url"] = url
    _write_env_mapping({"SUBSCRIPTION_URL": url})
    os.environ["SUBSCRIPTION_URL"] = url


def _activate_node_by_uri(uri: str, name: str, pool_index: int = 0) -> str:
    if not uri:
        raise HTTPException(status_code=400, detail="Node URI is empty")
    _runtime_state["node_pool_index"] = pool_index
    if needs_worker(uri):
        try:
            proxy_url = worker.start_with_uri(uri, name=name or "")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        _runtime_state["active_node_uri"] = uri
        _runtime_state["active_node_name"] = name or uri
        _set_proxy_url(proxy_url)
        return proxy_url
    if not any(uri.startswith(scheme) for scheme in _DIRECT_SCHEMES):
        raise HTTPException(status_code=400, detail=f"Unsupported node protocol: {uri[:20]}")
    worker.stop()
    _runtime_state["active_node_uri"] = uri
    _runtime_state["active_node_name"] = name or uri
    _set_proxy_url(uri)
    return uri


class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    port_api: Optional[int] = None
    debug: Optional[bool] = None
    max_retries: Optional[int] = None
    max_retries_429: Optional[int] = None
    retries_before_switch: Optional[int] = None
    proxy_url: Optional[str] = None
    vertex_location: Optional[str] = None
    admin_password: Optional[str] = None
    anti429_enabled: Optional[bool] = None
    anti429_target: Optional[str] = None
    force_no_stream: Optional[bool] = None
    proxy_route_enabled: Optional[bool] = None
    anti_tracking: Optional[bool] = None
    drop_max_tokens: Optional[bool] = None


class KeyBody(BaseModel):
    name: str
    key: str
    description: str = ""


class ModelsBody(BaseModel):
    models: list[str] | None = None
    alias_map: dict[str, str] | None = None


class SubscribeBody(BaseModel):
    url: str


class UseNodeBody(BaseModel):
    raw_uri: str
    name: str = ""


class NodePoolBody(BaseModel):
    pool: list[dict[str, Any]]


@router.get("/admin")
async def admin_page() -> FileResponse:
    index = STATIC_DIR / "admin.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="admin.html not found")
    return FileResponse(str(index), media_type="text/html; charset=utf-8")


@router.post("/api/admin/login")
async def admin_login(body: LoginBody) -> dict[str, Any]:
    expected = _get_admin_password()
    if not expected:
        raise HTTPException(status_code=500, detail="Admin password is not configured")
    if body.password != expected:
        raise HTTPException(status_code=401, detail="Invalid password")
    tok = _issue_token()
    return {"token": tok, "ttl_seconds": SESSION_TTL}


@router.post("/api/admin/logout")
async def admin_logout(request: Request) -> dict[str, str]:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        _sessions.pop(auth[7:].strip(), None)
    return {"status": "ok"}


@router.get("/api/admin/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    _require_auth(request)
    env_proxy = os.environ.get("PROXY_URL", "")
    return {
        "port_api": _runtime_state.get("port_api", 8050),
        "debug": bool(_runtime_state.get("debug", False)),
        "max_retries": int(getattr(app_config, "MAX_RETRIES_429", 6)),
        "max_retries_429": int(getattr(app_config, "MAX_RETRIES_429", 6)),
        "retries_before_switch": int(getattr(app_config, "RETRIES_BEFORE_SWITCH", 1)),
        "vertex_location": _runtime_state.get("vertex_location", getattr(app_config, "VERTEX_LOCATION", "global")),
        "proxy_url": _runtime_state.get("proxy_url", ""),
        "env_proxy_url_override": env_proxy,
        "admin_password_env_locked": False,
        "anti429_enabled": bool(_runtime_state.get("anti429_enabled", False)),
        "anti429_target": _runtime_state.get("anti429_target", "system"),
        "force_no_stream": bool(_runtime_state.get("force_no_stream", False)),
        "proxy_route_enabled": bool(_runtime_state.get("proxy_route_enabled", True)),
        "anti_tracking": bool(_runtime_state.get("proxy_route_enabled", True)),
        "drop_max_tokens": bool(_runtime_state.get("drop_max_tokens", False)),
    }


@router.put("/api/admin/settings")
async def update_settings(body: SettingsBody, request: Request) -> dict[str, Any]:
    _require_auth(request)
    notes: list[str] = []
    if body.port_api is not None:
        _runtime_state["port_api"] = body.port_api
        notes.append("Port change requires a service restart")
    if body.debug is not None:
        _runtime_state["debug"] = bool(body.debug)
        notes.append("Debug mode change requires restart for full effect")
    if body.max_retries is not None:
        body.max_retries_429 = body.max_retries
    if body.max_retries_429 is not None:
        value = max(0, int(body.max_retries_429))
        _runtime_state["max_retries_429"] = value
        app_config.MAX_RETRIES_429 = value
        _write_env_mapping({"MAX_RETRIES_429": str(value)})
    if body.retries_before_switch is not None:
        value = max(1, int(body.retries_before_switch))
        _runtime_state["retries_before_switch"] = value
        app_config.RETRIES_BEFORE_SWITCH = value
        _write_env_mapping({"RETRIES_BEFORE_SWITCH": str(value)})
    if body.proxy_url is not None:
        _set_proxy_url(body.proxy_url.strip())
    if body.vertex_location is not None:
        value = body.vertex_location.strip() or "global"
        _runtime_state["vertex_location"] = value
        app_config.VERTEX_LOCATION = value
        _write_env_mapping({"VERTEX_LOCATION": value})
    if body.admin_password is not None:
        if len(body.admin_password.strip()) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        _write_env_mapping({"ADMIN_PASSWORD": body.admin_password.strip()})
        os.environ["ADMIN_PASSWORD"] = body.admin_password.strip()
        notes.append("Admin password updated")
    if body.anti429_enabled is not None:
        value = bool(body.anti429_enabled)
        _runtime_state["anti429_enabled"] = value
        app_config.ANTI429_ASSIST = value
        _write_env_mapping({"ANTI429_ASSIST": "true" if value else "false"})
    if body.anti429_target is not None:
        _runtime_state["anti429_target"] = body.anti429_target
    if body.force_no_stream is not None:
        _runtime_state["force_no_stream"] = bool(body.force_no_stream)
    proxy_route_value = body.proxy_route_enabled
    if proxy_route_value is None and body.anti_tracking is not None:
        proxy_route_value = body.anti_tracking
    if proxy_route_value is not None:
        value = bool(proxy_route_value)
        _runtime_state["proxy_route_enabled"] = value
        _runtime_state["anti_tracking"] = value
        app_config.PROXY_ROUTE_ENABLED = value
        _write_env_mapping({"PROXY_ROUTE_ENABLED": "true" if value else "false"})
    if body.drop_max_tokens is not None:
        value = bool(body.drop_max_tokens)
        _runtime_state["drop_max_tokens"] = value
        app_config.DROP_MAX_TOKENS = value
        _write_env_mapping({"DROP_MAX_TOKENS": "true" if value else "false"})
    return {"status": "ok", "notes": notes}


@router.get("/api/admin/keys")
async def get_keys(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {"keys": _read_express_keys()}


@router.post("/api/admin/keys")
async def add_key(body: KeyBody, request: Request) -> dict[str, str]:
    _require_auth(request)
    name = body.name.strip()
    key = body.key.strip()
    if not name or not key:
        raise HTTPException(status_code=400, detail="name / key required")
    keys = [k for k in _read_express_keys() if k["name"] != name]
    keys.append({"name": name, "key": key, "description": body.description or ""})
    _write_express_keys(keys)
    request.app.state.express_key_manager.refresh_keys()
    return {"status": "ok"}


@router.delete("/api/admin/keys/{name}")
async def delete_key(name: str, request: Request) -> dict[str, str]:
    _require_auth(request)
    keys = _read_express_keys()
    new_keys = [k for k in keys if k["name"] != name]
    if len(new_keys) == len(keys):
        raise HTTPException(status_code=404, detail="Key not found")
    _write_express_keys(new_keys)
    request.app.state.express_key_manager.refresh_keys()
    return {"status": "ok"}


@router.get("/api/admin/models")
async def get_models(request: Request) -> dict[str, Any]:
    _require_auth(request)
    data = _read_models_json()
    return {"models": data.get("vertex_express_models", []), "alias_map": data.get("alias_map", {})}


@router.put("/api/admin/models")
async def update_models(body: ModelsBody, request: Request) -> dict[str, Any]:
    _require_auth(request)
    data = _read_models_json()
    if body.models is not None:
        cleaned = [m.strip() for m in body.models if m.strip()]
        data["vertex_models"] = cleaned
        data["vertex_express_models"] = cleaned
    if body.alias_map is not None:
        data["alias_map"] = {k.strip(): v.strip() for k, v in body.alias_map.items() if k.strip() and v.strip()}
    _write_models_json(data)
    return {"status": "ok"}


@router.post("/api/admin/subscribe")
async def fetch_subscription(body: SubscribeBody, request: Request) -> dict[str, Any]:
    _require_auth(request)
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Subscription URL must start with http(s)://")
    nodes = await _fetch_subscription(url)
    _set_subscription_url(url)
    return {"total": len(nodes), "usable_count": sum(1 for n in nodes if n.get("usable_as_proxy")), "nodes": nodes}


@router.get("/api/admin/subscription")
async def get_subscription(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {"url": _runtime_state.get("subscription_url", "")}


@router.post("/api/admin/use-node")
async def use_node(body: UseNodeBody, request: Request) -> dict[str, Any]:
    _require_auth(request)
    uri = body.raw_uri.strip()
    pool = _runtime_state.get("node_pool", [])
    pool_index = next((idx for idx, entry in enumerate(pool) if str(entry.get("raw_uri", "")).strip() == uri), 0)
    proxy_url = _activate_node_by_uri(uri, body.name or uri, pool_index=pool_index)
    via = "worker" if needs_worker(uri) else "direct"
    return {"status": "ok", "proxy_url": proxy_url, "via": via, "node_name": body.name}


@router.get("/api/admin/node-pool")
async def get_node_pool(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {"pool": _runtime_state.get("node_pool", []), "current_index": _runtime_state.get("node_pool_index", 0)}


@router.post("/api/admin/node-pool")
async def set_node_pool(body: NodePoolBody, request: Request) -> dict[str, Any]:
    _require_auth(request)
    pool = []
    for entry in body.pool:
        uri = str(entry.get("raw_uri", "")).strip()
        if uri:
            pool.append({"raw_uri": uri, "name": str(entry.get("name", ""))})
    _runtime_state["node_pool"] = pool
    _runtime_state["node_pool_index"] = 0
    return {"status": "ok", "count": len(pool)}


@router.post("/api/admin/stop-proxy")
async def stop_proxy(request: Request) -> dict[str, Any]:
    _require_auth(request)
    worker.stop()
    _runtime_state["active_node_uri"] = ""
    _runtime_state["active_node_name"] = ""
    _set_proxy_url("")
    return {"status": "ok"}


@router.get("/api/admin/proxy-status")
async def proxy_status(request: Request) -> dict[str, Any]:
    _require_auth(request)
    status = worker.status()
    status["configured_proxy_url"] = _runtime_state.get("proxy_url", "")
    status["active_node_uri"] = _runtime_state.get("active_node_uri", "")
    status["active_node_name"] = _runtime_state.get("active_node_name", "")
    return status
