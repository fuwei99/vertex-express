"""anti_truncation 的测试。测试向量取自真实探针（2026-10-14）的上游返回结构。

    python3 tests/test_anti_truncation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from anti_truncation import (  # noqa: E402
    SUFFIX, StreamState, prepare, resolve_enabled, restore_chunk, restore_response,
    skip_reason, strip_suffix,
)

FAILED = []


def check(label, actual, expected):
    if actual != expected:
        FAILED.append(f"{label}\n    期望: {expected!r}\n    实际: {actual!r}")
        print(f"  FAIL {label}\n       期望 {expected!r}\n       实际 {actual!r}")
    else:
        print(f"  ok   {label}")


class Request:
    def __init__(self, **kw):
        self.tools = kw.get("tools")
        self.tool_choice = kw.get("tool_choice")
        self.response_format = kw.get("response_format")
        self.n = kw.get("n")
        self.messages = kw.get("messages", [{"role": "user", "content": "hi"}])


print("== 1. 后缀解析 ==")
check("无后缀", strip_suffix("gemini-3.8-flash"), ("gemini-3.8-flash", False))
check("带后缀", strip_suffix("gemini-3.8-flash" + SUFFIX), ("gemini-3.8-flash", True))
check("只有后缀时不误伤", strip_suffix(SUFFIX), (SUFFIX, False))
check("中文模型名也认", strip_suffix("测试模型" + SUFFIX), ("测试模型", True))

print("== 2. 五个跳过条件（一个都不能少） ==")
check("无约束 → 不跳过", skip_reason(Request()), None)
check("客户端自带工具 → 跳过", skip_reason(Request(tools=[{"type": "function"}])), "existing-tools")
check("显式 tool_choice → 跳过", skip_reason(Request(tool_choice="auto")), "tool-choice")
check("结构化输出 → 跳过",
      skip_reason(Request(response_format={"type": "json_schema"})), "structured-output")
check("response_format=text → 不跳过",
      skip_reason(Request(response_format={"type": "text"})), None)
check("多候选 → 跳过", skip_reason(Request(n=3)), "multiple-candidates")
check("n=1 → 不跳过", skip_reason(Request(n=1)), None)
check("工具历史(role=tool) → 跳过",
      skip_reason(Request(messages=[{"role": "tool", "content": "x"}])), "tool-history")
check("工具历史(tool_calls) → 跳过",
      skip_reason(Request(messages=[{"role": "assistant", "tool_calls": [{}]}])), "tool-history")

print("== 3. 触发优先级：models.json > 后缀 > 全局开关 ==")
check("models.json 显式关，即使带后缀也关",
      resolve_enabled({"antiTruncation": False}, True, True), (False, "models.json"))
check("models.json 显式开，即使全局关也开",
      resolve_enabled({"antiTruncation": True}, False, False), (True, "models.json"))
check("无覆盖 + 带后缀 → 开（即使全局关）",
      resolve_enabled({}, True, False), (True, "suffix"))
check("无覆盖 + 无后缀 → 跟随全局开",
      resolve_enabled({}, False, True), (True, "global"))
check("无覆盖 + 无后缀 → 跟随全局关",
      resolve_enabled({}, False, False), (False, "global"))
check("models.json 为 None 不炸", resolve_enabled(None, False, True), (True, "global"))

print("== 4. 请求改写 ==")
contents = [{"role": "user", "parts": [{"text": "写20行"}]}]
config = {"temperature": 1.0, "tools": [{"function_declarations": [{"name": "real_tool"}]}]}
tool, new_contents, new_config = prepare(contents, config)

check("生成了合成工具名", tool.startswith("router_emit_"), True)
check("原 contents 未被改动", len(contents), 1)
check("追加了一条指令消息", len(new_contents), 2)
check("指令里点名了合成工具", tool in new_contents[1]["parts"][0]["text"], True)
check("原 config 未被改动", len(config["tools"]), 1)
check("原有真实工具被保留", new_config["tools"][0]["function_declarations"][0]["name"], "real_tool")
check("追加了合成工具声明", len(new_config["tools"]), 2)
check("合成工具 schema 正确",
      new_config["tools"][1]["function_declarations"][0]["parameters"]["required"], ["content"])

fcc = new_config["tool_config"]["function_calling_config"]
check("强制 mode=ANY", fcc["mode"], "ANY")
check("只允许合成工具", fcc["allowed_function_names"], [tool])
check("打开了参数分片下发", fcc["stream_function_call_arguments"], True)

print("== 5. 流式还原（用探针拿到的真实 partialArgs 结构） ==")
state = StreamState(tool)
# 分片 1：真实探针里第一片带两整行
chunk1 = {"candidates": [{"content": {"parts": [{"functionCall": {
    "name": tool,
    "partialArgs": [{"jsonPath": "$.content",
                     "stringValue": "1. The river flows quietly.\n2. The river flows quietly.",
                     "willContinue": True}],
    "willContinue": True}}]}}]}
out1, text1 = restore_chunk(chunk1, state)
check("分片1 解出正文", text1,
      "1. The river flows quietly.\n2. The river flows quietly.")
check("分片1 里合成调用被移除，换成 text part",
      [list(p.keys()) for p in out1["candidates"][0]["content"]["parts"]], [["text"]])

# 分片 2：半行 + willContinue=False
chunk2 = {"candidates": [{"content": {"parts": [{"functionCall": {
    "name": tool,
    "partialArgs": [{"jsonPath": "$.content", "stringValue": "3. The river",
                     "willContinue": False}],
    "willContinue": False}}]},
    "finishReason": "STOP"}]}
out2, text2 = restore_chunk(chunk2, state)
check("分片2 解出正文", text2, "3. The river")
check("调用已收完", state.call_closed, True)
check("累计正文", state.emitted,
      "1. The river flows quietly.\n2. The river flows quietly.3. The river")

print("== 6. 真实工具调用必须原样保留 ==")
state2 = StreamState(tool)
chunk_real = {"candidates": [{"content": {"parts": [{"functionCall": {
    "name": "some_real_tool", "args": {"x": 1}}}]}}]}
out3, text3 = restore_chunk(chunk_real, state2)
check("真实调用没被吃掉", text3, "")
check("真实调用仍在 parts 里",
      out3["candidates"][0]["content"]["parts"][0]["functionCall"]["name"], "some_real_tool")

print("== 7. finish_reason 改写 ==")
state3 = StreamState(tool)
state3.call_seen = True
chunk3 = {"candidates": [{"content": {"parts": []}, "finishReason": "FUNCTION_CALL"}]}
out4, _ = restore_chunk(chunk3, state3)
check("FUNCTION_CALL 改回 STOP", out4["candidates"][0]["finishReason"], "STOP")

print("== 8. 非流式还原 ==")
state4 = {"candidates": [{"content": {"parts": [{"functionCall": {
    "name": tool, "args": {"content": "完整回答正文"}}}]}, "finishReason": "STOP"}]}
out5, text5 = restore_response(state4, tool)
check("解出正文", text5, "完整回答正文")
check("合成调用被移除", [list(p.keys()) for p in out5["candidates"][0]["content"]["parts"]],
      [["text"]])

print("== 9. 边界：非 $.content 的 partialArgs 要忽略 ==")
state5 = StreamState(tool)
chunk5 = {"candidates": [{"content": {"parts": [{"functionCall": {
    "name": tool,
    "partialArgs": [{"jsonPath": "$.other", "stringValue": "不该出现", "willContinue": True}],
    "willContinue": True}}]}}]}
_, text6 = restore_chunk(chunk5, state5)
check("非 content 路径被忽略", text6, "")

if FAILED:
    print("\n失败 %d 项：" % len(FAILED))
    sys.exit(1)
print("\n全部通过")
