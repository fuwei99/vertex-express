import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from typing import List, Dict, Any, Set
from auth import get_api_key
from model_loader import get_vertex_models, get_vertex_express_models, refresh_models_config_cache
from credentials_manager import CredentialManager
from gemini_native_models import get_gemini_native_models

import config as app_config

router = APIRouter()

# alias_map 存在本地 vertexModels.json 里（控制台「模型管理」页写入）。
_VERTEX_MODELS_FILE = Path(__file__).resolve().parents[2] / "vertexModels.json"


def expand_model_variants() -> bool:
    """是否自动展开前缀/后缀变体。默认关；控制台开关或环境变量可打开。"""
    raw = os.environ.get("EXPAND_MODEL_VARIANTS")
    if raw is None or str(raw).strip() == "":
        return bool(getattr(app_config, "EXPAND_MODEL_VARIANTS", False))
    return str(raw).strip().lower() == "true"


def _registered_variants() -> Dict[str, str]:
    """映射里自己注册的变体：{客户端模型名: 目标基础模型}。读不到就返回空。"""
    try:
        data = json.loads(_VERTEX_MODELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    alias = data.get("alias_map")
    if not isinstance(alias, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in alias.items() if str(k).strip()}


@router.get("/v1beta/models")
async def list_gemini_models(api_key: str = Depends(get_api_key)):
    models = get_gemini_native_models()
    return {
        "models": [
            {
                "name": f"models/{model['id']}",
                "baseModelId": model.get("baseModelId", model["id"]),
                "version": "001",
                "displayName": model.get("displayName", model["id"]),
                "description": model.get("description", model["id"]),
                "inputTokenLimit": 1048576,
                "outputTokenLimit": 65536,
                "supportedGenerationMethods": model.get(
                    "supportedGenerationMethods",
                    [
                        "generateContent",
                        "streamGenerateContent",
                    ],
                ),
            }
            for model in models
        ]
    }

@router.get("/v1/models")
async def list_models(fastapi_request: Request, api_key: str = Depends(get_api_key)):
    await refresh_models_config_cache()
    
    PAY_PREFIX = "[PAY]"
    EXPRESS_PREFIX = "[EXPRESS] "
    OPENAI_DIRECT_SUFFIX = "-openai"
    OPENAI_SEARCH_SUFFIX = "-openaisearch"
    
    credential_manager_instance: CredentialManager = fastapi_request.app.state.credential_manager
    express_key_manager_instance = fastapi_request.app.state.express_key_manager

    has_sa_creds = credential_manager_instance.get_total_credentials() > 0
    has_express_key = express_key_manager_instance.get_total_keys() > 0

    raw_vertex_models = await get_vertex_models()
    raw_express_models = await get_vertex_express_models()
    
    final_model_list: List[Dict[str, Any]] = []
    processed_ids: Set[str] = set()
    current_time = int(time.time())

    # 默认不再自动展开前缀/后缀变体（[EXPRESS]/[PAY]、-search/-encrypt/-auto/
    # -nothinking/-max/-2k/-4k/-openai…）。只列**显式存在**的模型：
    #   ① vertexModels.json 里的基础模型
    #   ② models.json 里已写好的条目（思考等级 -low/-high、图片等级 -2K/-4K 等）
    #   ③ alias_map 里自己注册的映射
    # 需要哪个变体就在映射里注册一条，不需要就不会出现。
    # 想回到旧的自动展开行为：控制台打开「展开模型变体」开关，或设
    # EXPAND_MODEL_VARIANTS=true。旧代码原样保留在下面。
    if not expand_model_variants():
        explicit: List[tuple[str, str]] = []
        seen_base: Set[str] = set()
        for model_id in list(raw_express_models) + list(raw_vertex_models):
            if model_id not in seen_base:
                seen_base.add(model_id)
                explicit.append((model_id, model_id))
        for entry in get_gemini_native_models():
            explicit.append((entry["id"], str(entry.get("baseModelId") or entry["id"])))
        for client_id, target in _registered_variants().items():
            explicit.append((client_id, target))
        for client_id, root in explicit:
            if client_id in processed_ids:
                continue
            processed_ids.add(client_id)
            final_model_list.append({
                "id": client_id,
                "object": "model",
                "created": current_time,
                "owned_by": "google",
                "permission": [],
                "root": root,
                "parent": None,
            })
        return {"object": "list", "data": sorted(final_model_list, key=lambda x: x['id'])}

    def add_model_and_variants(base_id: str, prefix: str):
        """Adds a model and its variants to the list if not already present."""
        
        # Define all possible suffixes for a given model
        suffixes = [""] # For the base model itself
        if not base_id.startswith("gemini-2.0"):
            suffixes.extend(["-search", "-encrypt", "-encrypt-full", "-auto"])
        if ("gemini-2.5-flash" in base_id or "gemini-2.5-pro" == base_id or "gemini-2.5-pro-preview-06-05" == base_id or "gemini-3-pro" in base_id) and "gemini-2.5-flash-image" not in base_id:
            suffixes.extend(["-nothinking", "-max"])
        if ("gemini-3-pro-image") in base_id:
            suffixes.extend(["-2k", "-4k"]) 
        
        # Add the openai variant for all models
        suffixes.append(OPENAI_DIRECT_SUFFIX)
        suffixes.append(OPENAI_SEARCH_SUFFIX)

        for suffix in suffixes:
            model_id_with_suffix = f"{base_id}{suffix}"
            
            # Experimental models have no prefix
            final_id = f"{prefix}{model_id_with_suffix}" if "-exp-" not in base_id else model_id_with_suffix

            if final_id not in processed_ids:
                final_model_list.append({
                    "id": final_id,
                    "object": "model",
                    "created": current_time,
                    "owned_by": "google",
                    "permission": [],
                    "root": base_id,
                    "parent": None
                })
                processed_ids.add(final_id)

    # Process Express Key models first
    if has_express_key:
        for model_id in raw_express_models:
            add_model_and_variants(model_id, EXPRESS_PREFIX)

    # Process Service Account (PAY) models, they have lower priority
    if has_sa_creds:
        for model_id in raw_vertex_models:
            add_model_and_variants(model_id, PAY_PREFIX)

    return {"object": "list", "data": sorted(final_model_list, key=lambda x: x['id'])}
