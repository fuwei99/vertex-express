import json
import re
import os
import asyncio
import urllib.request
import urllib.error
from typing import Dict, Optional
import config_store
import config

# Global cache for project IDs: {api_key: project_id}
_raw_cache = os.environ.get("PROJECT_ID_MAP", "{}")
try:
    PROJECT_ID_CACHE: Dict[str, str] = json.loads(_raw_cache)
    if not isinstance(PROJECT_ID_CACHE, dict):
        PROJECT_ID_CACHE = {}
except Exception:
    PROJECT_ID_CACHE = {}


def _get_proxy_url() -> Optional[str]:
    """Get proxy URL from config or environment."""
    return os.environ.get("PROXY_URL") or config.PROXY_URL


async def discover_project_id(api_key: str) -> str:
    """
    Discover project ID by triggering an intentional error using standard urllib.
    Force direct connection to avoid depending on the local proxy before it is ready.
    """
    if api_key in PROJECT_ID_CACHE:
        return PROJECT_ID_CACHE[api_key]
    
    error_url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-2.7-pro-preview-05-06:streamGenerateContent?key={api_key}"
    payload = json.dumps({"contents": [{"role": "user", "parts": [{"text": "test"}]}]}).encode("utf-8")
    
    def _fetch() -> tuple[int, str]:
        proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(
            error_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        try:
            with opener.open(req, timeout=15.0) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")
        except Exception as e:
            raise Exception(f"Connection failed: {str(e)}")

    try:
        status_code, response_text = await asyncio.to_thread(_fetch)
        match = re.search(r"projects/(\d+)/locations/", response_text)
        if match:
            project_id = match.group(1)
            PROJECT_ID_CACHE[api_key] = project_id
            print(f"INFO: Discovered project ID (Direct): {project_id}")
            
            # Persist to config.json
            try:
                config_store.write_config_values({"PROJECT_ID_MAP": PROJECT_ID_CACHE})
            except Exception as e:
                print(f"WARNING: Failed to persist PROJECT_ID_MAP: {e}")
                
            return project_id

        raise Exception(f"Failed to discover project ID. Status: {status_code}, Response: {response_text[:500]}")

    except Exception as e:
        print(f"ERROR: Failed to discover project ID: {e}")
        raise
