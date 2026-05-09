import httpx
import asyncio
import json
from typing import List, Dict, Optional, Any

# Assuming config.py is in the same directory level for Docker execution
import config as app_config 

_model_cache: Optional[Dict[str, List[str]]] = None
_cache_lock = asyncio.Lock()

from pathlib import Path
from config_store import ROOT_DIR

LOCAL_MODELS_FILE = ROOT_DIR / "vertexModels.json"

def _validate_config(data: Any) -> bool:
    return isinstance(data, dict) and \
           "vertex_models" in data and isinstance(data["vertex_models"], list) and \
           "vertex_express_models" in data and isinstance(data["vertex_express_models"], list)

async def fetch_and_parse_models_config() -> Optional[Dict[str, List[str]]]:
    """
    Fetches the model configuration. 
    Priority: 1. Local vertexModels.json, 2. Remote URL from app_config.
    """
    # 1. Try local file first
    if LOCAL_MODELS_FILE.exists():
        print(f"Loading model configuration from local file: {LOCAL_MODELS_FILE}")
        try:
            data = json.loads(LOCAL_MODELS_FILE.read_text(encoding="utf-8"))
            if _validate_config(data):
                print("Successfully loaded model configuration from local file.")
                return data
            else:
                print(f"ERROR: Local model configuration has an invalid structure.")
        except Exception as e:
            print(f"ERROR: Failed to read or parse local model configuration: {e}")
    
    # 2. Fallback to remote URL
    if not app_config.MODELS_CONFIG_URL:
        print("ERROR: MODELS_CONFIG_URL is not set and local file is missing.")
        return None

    print(f"Fetching model configuration from remote: {app_config.MODELS_CONFIG_URL}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(app_config.MODELS_CONFIG_URL)
            response.raise_for_status()
            data = response.json()
            
            if _validate_config(data):
                print("Successfully fetched and parsed remote model configuration.")
                return data
            else:
                print(f"ERROR: Remote model configuration has an invalid structure: {data}")
                return None
    except Exception as e:
        print(f"ERROR: Failed to fetch/parse remote model configuration: {e}")
        return None

async def get_models_config() -> Dict[str, List[str]]:
    """
    Returns the cached model configuration.
    If not cached, fetches and caches it.
    Returns a default empty structure if fetching fails.
    """
    global _model_cache
    async with _cache_lock:
        if _model_cache is None:
            print("Model cache is empty. Fetching configuration...")
            _model_cache = await fetch_and_parse_models_config()
            if _model_cache is None: # If fetching failed, use a default empty structure
                print("WARNING: Using default empty model configuration due to fetch/parse failure.")
                _model_cache = {"vertex_models": [], "vertex_express_models": []}
    return _model_cache

async def get_vertex_models() -> List[str]:
    config = await get_models_config()
    return config.get("vertex_models", [])

async def get_vertex_express_models() -> List[str]:
    config = await get_models_config()
    return config.get("vertex_express_models", [])

async def refresh_models_config_cache() -> bool:
    """
    Forces a refresh of the model configuration cache.
    Returns True if successful, False otherwise.
    """
    global _model_cache
    print("Attempting to refresh model configuration cache...")
    async with _cache_lock:
        new_config = await fetch_and_parse_models_config()
        if new_config is not None:
            _model_cache = new_config
            print("Model configuration cache refreshed successfully.")
            return True
        else:
            print("ERROR: Failed to refresh model configuration cache.")
            # Optionally, decide if we want to clear the old cache or keep it
            # _model_cache = {"vertex_models": [], "vertex_express_models": []} # To clear
            return False