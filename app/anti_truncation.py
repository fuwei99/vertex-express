"""工具调用抗截断传输（Express / 原生 Vertex 通道）。

复刻 ken050210/vertex-streaming-anti-truncation 的思路，但**在 Gemini 原生层实现**，
因此不需要原项目那个手写的增量 JSON 状态机 —— 上游给的是结构化的
`functionCall.partialArgs`（带 `jsonPath` / `stringValue`），直接拼即可。

机制：
  1. 注入合成工具 `router_emit_<hex>`，schema = {content: string}
  2. 追加一条 user 指令：只调用它一次，把整段回答放进 content
  3. 强制 toolConfig.functionCallingConfig = {mode: ANY,
     allowedFunctionNames: [name], streamFunctionCallArguments: true}
  4. 上游以 partialArgs 分片下发 → 把 jsonPath == "$.content" 的 stringValue
     拼起来，塞回 parts[].text → 下游 convert_chunk_to_openai 照常转成 content 增量

触发优先级（高 → 低）：
  models.json 的 antiTruncation 字段  >  模型名后缀 -antitrunc  >  控制台全局开关

安全边界：真实工具调用 / 显式 tool_choice / 结构化输出 / 多候选 / 工具历史
一律跳过 —— 抗截断会抢走 tool_choice，套上去会打坏这些请求。
"""

import copy
import json
import secrets
from typing import Any, Optional

SUFFIX = "-antitrunc"

# 内容字段名。上游 partialArgs 的 jsonPath 会是 "$.content"。
CONTENT_FIELD = "content"

_INSTRUCTION = (
    "Transport format for this reply: call {tool} exactly once. Put the entire "
    "user-visible answer in its content string, preserving all requested formatting, "
    "markup, language and sections. The function only transports text and performs "
    "no external action."
)

_CONTINUATION_INSTRUCTION = (
    "\nFollow the existing continuation context and extend the message being continued "
    "from its exact end. Output only the new text to append; do not repeat, summarize, "
    "rewrite or restart the existing text. Preserve its language, point of view, "
    "formatting and any unfinished sentence or markup. Do not add a preamble, "
    "continuation label, speaker prefix or enclosing quotes unless they belong to the "
    "continuation itself."
)


# --------------------------------------------------------------------------- #
# 触发判定
# --------------------------------------------------------------------------- #

def strip_suffix(model: str) -> tuple[str, bool]:
    """拆出 -antitrunc 后缀。返回 (基础模型名, 是否带后缀)。"""
    name = str(model or "").strip()
    if name.endswith(SUFFIX) and len(name) > len(SUFFIX):
        return name[: -len(SUFFIX)], True
    return name, False


def skip_reason(request: Any) -> Optional[str]:
    """返回跳过原因；None 表示可以做抗截断。

    这五条是原项目的边界，不能省：抗截断会抢走 tool_choice，套到带真实工具的
    请求上会让客户端的函数永远调不到。
    """
    tools = getattr(request, "tools", None)
    if tools:
        return "existing-tools"
    if getattr(request, "tool_choice", None) is not None:
        return "tool-choice"
    response_format = getattr(request, "response_format", None)
    if isinstance(response_format, dict) and response_format.get("type") not in (None, "text"):
        return "structured-output"
    if getattr(request, "n", None) not in (None, 1):
        return "multiple-candidates"
    for message in getattr(request, "messages", None) or []:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role in ("tool", "function"):
            return "tool-history"
        if isinstance(message, dict) and (message.get("tool_calls") or message.get("function_call")):
            return "tool-history"
    return None


def resolve_enabled(model_config: Optional[dict], forced: bool, global_default: bool) -> tuple[bool, str]:
    """按优先级算出该不该开。返回 (是否开启, 依据)。"""
    config = model_config if isinstance(model_config, dict) else {}
    if "antiTruncation" in config:
        value = bool(config["antiTruncation"])
        return value, "models.json"
    if forced:
        return True, "suffix"
    return bool(global_default), "global"


# --------------------------------------------------------------------------- #
# 请求改写
# --------------------------------------------------------------------------- #

def _tool_declaration(tool_name: str, continuation: bool) -> dict:
    return {
        "name": tool_name,
        "description": ("Deliver only the new continuation text to append to the existing message."
                        if continuation else "Deliver the complete user-visible reply as text."),
        "parameters": {
            "type": "object",
            "properties": {CONTENT_FIELD: {"type": "string"}},
            "required": [CONTENT_FIELD],
            "additionalProperties": False,
        },
    }


def prepare(contents: list[Any], config: dict[str, Any], *, continuation: bool = False):
    """注入合成工具与指令，打开原生参数流。

    返回 (tool_name, contents, config)。调用方负责把 tool_name 传给还原函数。
    """
    tool_name = "router_emit_" + secrets.token_hex(12)

    instruction = _INSTRUCTION.format(tool=tool_name)
    if continuation:
        instruction += _CONTINUATION_INSTRUCTION

    new_contents = list(contents or [])
    new_contents.append({"role": "user", "parts": [{"text": instruction}]})

    new_config = copy.deepcopy(config or {})
    tools = list(new_config.get("tools") or [])
    tools.append({"function_declarations": [_tool_declaration(tool_name, continuation)]})
    new_config["tools"] = tools
    # 强制只调这一个；stream_function_call_arguments 是参数分片下发的前提。
    new_config["tool_config"] = {
        "function_calling_config": {
            "mode": "ANY",
            "allowed_function_names": [tool_name],
            "stream_function_call_arguments": True,
        }
    }
    return tool_name, new_contents, new_config


# --------------------------------------------------------------------------- #
# 响应还原
# --------------------------------------------------------------------------- #

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与 AttrDict。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _set(obj: Any, key: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


def _text_part(text: str) -> dict:
    return {"text": text}


def _function_call_of(part: Any) -> Any:
    return _get(part, "functionCall") or _get(part, "function_call")


def _is_synthetic(part: Any, tool_name: str) -> bool:
    call = _function_call_of(part)
    return bool(call) and _get(call, "name") == tool_name


def _decode_partial_args(call: Any) -> tuple[str, bool]:
    """从 partialArgs 里拼出正文。返回 (文本, 是否已收完)。

    只认 jsonPath == "$.content"；上游已经做好 JSON 解码，直接拼 stringValue。
    """
    chunks = _get(call, "partialArgs") or _get(call, "partial_args") or []
    text = ""
    closed = _get(call, "willContinue") is False
    for item in chunks:
        path = _get(item, "jsonPath") or _get(item, "json_path")
        if path != "$." + CONTENT_FIELD:
            continue
        text += _get(item, "stringValue") or _get(item, "string_value") or ""
        if _get(item, "willContinue") is False or _get(item, "will_continue") is False:
            closed = True
    return text, closed


def _decode_full_args(call: Any) -> str:
    """完整 args 的情况（非流式，或上游没走分片）。"""
    args = _get(call, "args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return ""
    if isinstance(args, dict):
        value = args.get(CONTENT_FIELD)
        return value if isinstance(value, str) else ""
    return ""


class StreamState:
    """一次流式请求的还原状态。"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.text_started = False
        self.text_channel_taken = False
        self.call_seen = False
        self.call_closed = False
        self.emitted = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        # 第一个非空文本通道胜出，后续普通文本不再重复输出。
        if not self.text_channel_taken:
            self.text_channel_taken = True
        self.text_started = True
        self.emitted += text
        return text


def restore_chunk(payload: Any, state: StreamState) -> tuple[Any, str]:
    """把一个原生流式 chunk 里的合成 functionCall 改写成 text part。

    返回 (改写后的 payload, 本次新增的正文)。真实工具调用原样保留。
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return payload, ""

    emitted = ""
    for candidate in payload.get("candidates") or []:
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue

        kept: list[Any] = []
        decoded = ""
        for part in parts:
            if _is_synthetic(part, state.tool_name):
                state.call_seen = True
                call = _function_call_of(part)
                text, closed = _decode_partial_args(call)
                if not text:
                    text = _decode_full_args(call)
                    if text:
                        closed = True
                if closed:
                    state.call_closed = True
                decoded += text
                continue                      # 合成调用不下发
            kept.append(part)

        if decoded:
            emitted += state.feed(decoded)
            kept.append(_text_part(decoded))
        content["parts"] = kept

        # finish_reason 从 tool_calls 改回 stop：对客户端来说这就是一次普通文本回复。
        reason = candidate.get("finishReason") or candidate.get("finish_reason")
        if reason and state.call_seen and not any(
                _function_call_of(p) for p in kept):
            if str(reason).upper() in ("STOP", "FUNCTION_CALL", "TOOL_CODE"):
                if str(reason).upper() != "STOP":
                    candidate["finishReason" if "finishReason" in candidate else "finish_reason"] = "STOP"

    return payload, emitted


def restore_response(payload: Any, tool_name: str) -> tuple[Any, str]:
    """非流式还原：把合成 functionCall 的 args.content 变成普通 text part。"""
    if not isinstance(payload, dict) or payload.get("error"):
        return payload, ""

    text = ""
    for candidate in payload.get("candidates") or []:
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        kept: list[Any] = []
        found = False
        for part in parts:
            if _is_synthetic(part, tool_name):
                found = True
                text += _decode_full_args(_function_call_of(part))
                continue
            kept.append(part)
        if found:
            if text:
                kept.append(_text_part(text))
            content["parts"] = kept
            reason = candidate.get("finishReason") or candidate.get("finish_reason")
            if reason and str(reason).upper() in ("STOP", "FUNCTION_CALL", "TOOL_CODE"):
                candidate["finishReason" if "finishReason" in candidate else "finish_reason"] = "STOP"
    return payload, text


def describe(enabled: bool, basis: str, reason: Optional[str], tool_name: Optional[str]) -> str:
    if reason:
        return "[抗截断] 跳过（%s）" % reason
    if not enabled:
        return "[抗截断] 关闭（依据 %s）" % basis
    return "[抗截断] 开启（依据 %s，工具 %s）" % (basis, tool_name)
