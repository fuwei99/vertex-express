import base64
import asyncio
import json
import os
import re
import traceback
from enum import Enum
from typing import Any, AsyncGenerator, Optional

from curl_cffi import requests as curl_requests

import config as app_config
from node_manager import is_rate_limit_error, is_transient_proxy_error, switch_next_node


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
    proxy_url = os.environ.get("PROXY_URL") or app_config.PROXY_URL
    if not proxy_url:
        return None
    if proxy_url.startswith("socks5://"):
        proxy_url = "socks5h://" + proxy_url[len("socks5://"):]
    return {"http": proxy_url, "https": proxy_url}


def _proxy_log_value() -> str:
    return os.environ.get("PROXY_URL") or app_config.PROXY_URL or "None"


def _image_part_to_markdown(part: dict[str, Any]) -> Optional[dict[str, str]]:
    inline_data = part.get("inlineData") or part.get("inline_data")
    if isinstance(inline_data, dict):
        mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
        data = inline_data.get("data")
        if isinstance(mime_type, str) and mime_type.startswith("image/") and isinstance(data, str) and data:
            return {"text": f"![Image](data:{mime_type};base64,{data})"}

    file_data = part.get("fileData") or part.get("file_data")
    if isinstance(file_data, dict):
        mime_type = file_data.get("mimeType") or file_data.get("mime_type") or "image/png"
        file_uri = file_data.get("fileUri") or file_data.get("file_uri")
        if isinstance(mime_type, str) and mime_type.startswith("image/") and isinstance(file_uri, str) and file_uri:
            return {"text": f"![Image]({file_uri})"}

    return None


def convert_gemini_images_to_markdown(value: Any) -> Any:
    if isinstance(value, list):
        return [convert_gemini_images_to_markdown(item) for item in value]
    if not isinstance(value, dict):
        return value

    markdown_part = _image_part_to_markdown(value)
    if markdown_part is not None:
        return markdown_part

    return {
        key: convert_gemini_images_to_markdown(item)
        for key, item in value.items()
    }


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


async def generate_content_raw(
    ctx: GeminiRestClientContext,
    model: str,
    payload: dict[str, Any],
) -> Any:
    max_retries = max(0, int(getattr(app_config, "MAX_RETRIES_429", 6)))
    retries_before_switch = max(1, int(getattr(app_config, "RETRIES_BEFORE_SWITCH", 1)))
    retry_count = 0
    retries_on_current_node = 0

    while True:
        url = _build_url(ctx, model, "generateContent", stream=False)
        print(f"INFO: Executing raw Gemini REST call (curl_cffi) to '{model}'. Proxy: {_proxy_log_value()}")
        try:
            async with curl_requests.AsyncSession(impersonate="chrome124", proxies=_proxies()) as session:
                response = await session.post(url, json=payload, headers=_headers(ctx), timeout=300)
                await _raise_for_status_with_body(response)
                return convert_gemini_images_to_markdown(response.json())
        except Exception as exc:
            should_switch_node = is_rate_limit_error(exc) or is_transient_proxy_error(exc)
            if retry_count < max_retries and should_switch_node:
                retry_count += 1
                retries_on_current_node += 1
                print(f"WARNING: Retryable raw Gemini error {retry_count}/{max_retries} on current node attempt {retries_on_current_node}/{retries_before_switch}: {str(exc)[:800]}")
                if retries_on_current_node >= retries_before_switch:
                    if not await switch_next_node(f"retryable raw generate error while calling {model}: {type(exc).__name__}"):
                        raise
                    retries_on_current_node = 0
                await asyncio.sleep(0.2)
                continue
            raise


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


async def stream_generate_content_raw(
    ctx: GeminiRestClientContext,
    model: str,
    payload: dict[str, Any],
) -> AsyncGenerator[str, None]:
    max_retries = max(0, int(getattr(app_config, "MAX_RETRIES_429", 6)))
    retries_before_switch = max(1, int(getattr(app_config, "RETRIES_BEFORE_SWITCH", 1)))
    retry_count = 0
    retries_on_current_node = 0

    while True:
        yielded_content = False
        url = _build_url(ctx, model, "streamGenerateContent", stream=True)
        print(f"INFO: Executing raw Gemini REST stream (curl_cffi) to '{model}'. Proxy: {_proxy_log_value()}")
        try:
            async with curl_requests.AsyncSession(impersonate="chrome124", proxies=_proxies()) as session:
                async with session.stream("POST", url, json=payload, headers=_headers(ctx), timeout=300) as response:
                    await _raise_for_status_with_body(response)
                    async for line in response.aiter_lines():
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        line = line.strip()
                        if not line:
                            continue
                        yielded_content = True
                        if line.startswith("data:"):
                            raw_data = line[5:].strip()
                            if raw_data and raw_data != "[DONE]":
                                try:
                                    line = "data: " + json.dumps(convert_gemini_images_to_markdown(json.loads(raw_data)))
                                except json.JSONDecodeError:
                                    pass
                        elif not line.startswith(":"):
                            try:
                                line = "data: " + json.dumps(convert_gemini_images_to_markdown(json.loads(line)))
                            except json.JSONDecodeError:
                                pass
                        if line.startswith("data:") or line.startswith(":"):
                            yield f"{line}\n\n"
                        else:
                            yield f"data: {line}\n\n"
            return
        except Exception as exc:
            should_switch_node = is_rate_limit_error(exc) or is_transient_proxy_error(exc)
            if not yielded_content and retry_count < max_retries and should_switch_node:
                retry_count += 1
                retries_on_current_node += 1
                print(f"WARNING: Retryable raw Gemini stream error {retry_count}/{max_retries} on current node attempt {retries_on_current_node}/{retries_before_switch}: {str(exc)[:800]}")
                if retries_on_current_node >= retries_before_switch:
                    if await switch_next_node(f"retryable raw stream error while calling {model}: {type(exc).__name__}"):
                        retries_on_current_node = 0
                        await asyncio.sleep(0.2)
                        continue
                else:
                    await asyncio.sleep(0.2)
                    continue

            print(f"ERROR: Raw Gemini REST stream failed for model '{model}': {type(exc).__name__} - {exc}")
            traceback.print_exc()
            yield f"data: {json.dumps({'error': {'message': str(exc)}})}\n\n"
            return
