import base64
import json
import re
from enum import Enum
from typing import Any, AsyncGenerator, Optional

from curl_cffi import requests as curl_requests

import config as app_config


class GeminiRestClientContext:
    def __init__(
        self,
        project_id: str,
        location: str = "global",
        api_key: Optional[str] = None,
        bearer_token: Optional[str] = None,
    ):
        self.project_id = project_id
        self.location = location
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.model_name = "gemini_rest_client"


class EnumValue(str):
    @property
    def name(self) -> str:
        return str(self)


class AttrDict(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _snake_to_camel(key: str) -> str:
    if "_" not in key:
        return key
    first, *rest = key.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def _camel_to_snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def _to_jsonable(value: Any, camel_keys: bool = True) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_jsonable(item, camel_keys=camel_keys) for item in value if item is not None]
    if isinstance(value, tuple):
        return [_to_jsonable(item, camel_keys=camel_keys) for item in value if item is not None]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            if raw_val is None:
                continue
            key = _snake_to_camel(str(raw_key)) if camel_keys else str(raw_key)
            out[key] = _to_jsonable(raw_val, camel_keys=camel_keys)
        return out
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump(exclude_none=True), camel_keys=camel_keys)
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value), camel_keys=camel_keys)
    return value


def _wrap_response(value: Any, key_hint: str = "") -> Any:
    if isinstance(value, list):
        return [_wrap_response(item, key_hint=key_hint) for item in value]
    if isinstance(value, dict):
        wrapped = AttrDict()
        for raw_key, raw_val in value.items():
            key = _camel_to_snake(str(raw_key))
            wrapped[key] = _wrap_response(raw_val, key_hint=key)
        return wrapped
    if key_hint in {"category", "probability", "severity", "finish_reason", "block_reason"} and isinstance(value, str):
        return EnumValue(value)
    return value


def _split_rest_payload(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    payload: dict[str, Any] = {}

    special_map = {
        "safety_settings": "safetySettings",
        "tools": "tools",
        "tool_config": "toolConfig",
        "system_instruction": "systemInstruction",
    }
    generation_config: dict[str, Any] = {}

    for key, val in cfg.items():
        if val is None:
            continue
        if key in special_map:
            if key == "system_instruction" and isinstance(val, str):
                payload[special_map[key]] = {"parts": [{"text": val}]}
            else:
                payload[special_map[key]] = _to_jsonable(val, camel_keys=True)
        else:
            generation_config[_snake_to_camel(key)] = _to_jsonable(val, camel_keys=True)

    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def build_payload(contents: list[Any], config: dict[str, Any]) -> dict[str, Any]:
    payload = {"contents": _to_jsonable(contents, camel_keys=True)}
    payload.update(_split_rest_payload(config))
    return payload


def _build_url(ctx: GeminiRestClientContext, model: str, action: str, stream: bool) -> str:
    url = (
        "https://aiplatform.googleapis.com/v1/"
        f"projects/{ctx.project_id}/locations/{ctx.location}/publishers/google/models/{model}:{action}"
    )
    params: list[str] = []
    if stream:
        params.append("alt=sse")
    if ctx.api_key:
        params.append(f"key={ctx.api_key}")
    if params:
        url += "?" + "&".join(params)
    return url


def _headers(ctx: GeminiRestClientContext) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if ctx.bearer_token:
        headers["Authorization"] = f"Bearer {ctx.bearer_token}"
    if ctx.api_key:
        headers["x-goog-api-key"] = ctx.api_key
    return headers


def _proxies() -> Optional[dict[str, str]]:
    proxy_url = app_config.PROXY_URL
    if not proxy_url:
        return None
    if proxy_url.startswith("socks5://"):
        proxy_url = "socks5h://" + proxy_url[len("socks5://"):]
    return {"http": proxy_url, "https": proxy_url}


async def _raise_for_status_with_body(response: Any) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 400:
        return

    body = ""
    try:
        body = await response.atext()
    except Exception:
        try:
            body = str(getattr(response, "text", "") or "")
        except Exception:
            body = ""

    body = " ".join(body.split())
    if len(body) > 1200:
        body = body[:1200] + "..."
    raise RuntimeError(f"HTTP {status_code} from Gemini REST: {body}")


async def generate_content(
    ctx: GeminiRestClientContext,
    model: str,
    contents: list[Any],
    config: dict[str, Any],
) -> Any:
    payload = build_payload(contents, config)
    url = _build_url(ctx, model, "generateContent", stream=False)
    print(f"INFO: Executing Gemini REST call (curl_cffi) to '{model}'. Proxy: {app_config.PROXY_URL or 'None'}")
    async with curl_requests.AsyncSession(impersonate="chrome124", proxies=_proxies()) as session:
        response = await session.post(url, json=payload, headers=_headers(ctx), timeout=300)
        await _raise_for_status_with_body(response)
        return _wrap_response(response.json())


async def stream_generate_content(
    ctx: GeminiRestClientContext,
    model: str,
    contents: list[Any],
    config: dict[str, Any],
) -> AsyncGenerator[Any, None]:
    payload = build_payload(contents, config)
    url = _build_url(ctx, model, "streamGenerateContent", stream=True)
    print(f"INFO: Executing Gemini REST stream (curl_cffi) to '{model}'. Proxy: {app_config.PROXY_URL or 'None'}")
    async with curl_requests.AsyncSession(impersonate="chrome124", proxies=_proxies()) as session:
        async with session.stream("POST", url, json=payload, headers=_headers(ctx), timeout=300) as response:
            await _raise_for_status_with_body(response)
            async for line in response.aiter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    continue
                try:
                    yield _wrap_response(json.loads(line))
                except json.JSONDecodeError:
                    print(f"WARNING: Could not decode Gemini REST stream line: {line[:200]}")
