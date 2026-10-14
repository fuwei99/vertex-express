import json
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
MODELS_FILE = ROOT_DIR / "models.json"

# 只在代理内部消费、绝不能发往上游的配置键。
#
# ⚠️ 任何新增的「读 models.json 但不属于 Gemini 请求体」的字段，都必须加进这里。
# 模型条目里的其它键会被 deep_merge 原样并进上游 body，Google 对多出来的字段
# 是零容忍的：直接 400 Invalid JSON payload ... Unknown name "xxx"。
# 历史上就栽过一次：区域规则的 excludeLocations 漏了，gemini-4-argon 全线 400，
# 而客户端因为流式响应头已经发出，看到的是 200 + 空回复。
PROXY_ONLY_KEYS = frozenset({
    "id",
    "name",
    "baseModelId",
    "displayName",
    "description",
    "api",
    "location",
    "locations",
    "excludeLocations",
    "supportedGenerationMethods",
    "antiTruncation",
})

# Gemini 请求体里合法的顶层字段。只用来兜底告警（不过滤），
# 这样以后再加代理专用字段时，日志会直接点出来，而不是等 Google 报 400。
GEMINI_PAYLOAD_KEYS = frozenset({
    "contents",
    "tools",
    "toolConfig",
    "safetySettings",
    "systemInstruction",
    "generationConfig",
    "cachedContent",
    "labels",
})


def _read_models_json() -> dict[str, Any]:
    if not MODELS_FILE.exists():
        return {"models": []}
    return json.loads(MODELS_FILE.read_text(encoding="utf-8"))


def get_gemini_native_models() -> list[dict[str, Any]]:
    data = _read_models_json()
    models = data.get("models", [])
    return [model for model in models if isinstance(model, dict) and model.get("id")]


def get_gemini_native_model(model_id: str) -> dict[str, Any] | None:
    for model in get_gemini_native_models():
        if model.get("id") == model_id:
            return model
    return None


def deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def apply_native_model_config(model_id: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    model = get_gemini_native_model(model_id)
    if not model:
        return model_id, payload

    base_model_id = str(model.get("baseModelId") or model_id)
    payload_base = deepcopy(payload)
    payload_updates = {
        key: value
        for key, value in model.items()
        if key not in PROXY_ONLY_KEYS
    }

    # 兜底：代理专用字段漏进 payload 时，Google 只会回一句含糊的 Unknown name。
    # 在这里先把可疑字段点名，省得再去翻 models.json。
    unexpected = sorted(key for key in payload_updates if key not in GEMINI_PAYLOAD_KEYS)
    if unexpected:
        print(
            "[模型配置] 警告：模型 '%s' 的字段 %s 不是已知的 Gemini 请求体字段，"
            "会被原样发往上游。若上游回 Unknown name，请把它加进 PROXY_ONLY_KEYS。"
            % (model_id, ", ".join(unexpected))
        )

    update_thinking_config = (
        isinstance(payload_updates.get("generationConfig"), dict)
        and isinstance(payload_updates["generationConfig"].get("thinkingConfig"), dict)
    )
    if update_thinking_config and isinstance(payload_base.get("generationConfig"), dict):
        payload_base["generationConfig"] = dict(payload_base["generationConfig"])
        payload_base["generationConfig"].pop("thinkingConfig", None)

    return base_model_id, deep_merge(payload_base, payload_updates)
