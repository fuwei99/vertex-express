import json
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
MODELS_FILE = ROOT_DIR / "models.json"


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
        if key not in {
            "id",
            "name",
            "baseModelId",
            "displayName",
            "description",
            "api",
            "location",
            "supportedGenerationMethods",
        }
    }

    update_thinking_config = (
        isinstance(payload_updates.get("generationConfig"), dict)
        and isinstance(payload_updates["generationConfig"].get("thinkingConfig"), dict)
    )
    if update_thinking_config and isinstance(payload_base.get("generationConfig"), dict):
        payload_base["generationConfig"] = dict(payload_base["generationConfig"])
        payload_base["generationConfig"].pop("thinkingConfig", None)

    return base_model_id, deep_merge(payload_base, payload_updates)
