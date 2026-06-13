import os
import time
from typing import Any, Optional
import httpx

from routes.admin_ui import _activate_node_by_uri, _fetch_subscription, _runtime_state


def _node_priority(node: dict[str, Any]) -> int:
    node_type = str(node.get("type", "")).lower()
    raw_uri = str(node.get("raw_uri", "")).lower()
    if node_type in ("vless", "a") or raw_uri.startswith("vless://"):
        return 0
    return 1


def _sync_proxy_env(proxy_url: str) -> None:
    os.environ["PROXY_URL"] = proxy_url
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url


async def test_proxy_latency(proxy_url: str, timeout_seconds: float = 0.8) -> Optional[float]:
    """
    Test the latency of a proxy URL (e.g. socks5://127.0.0.1:20808).
    Returns the latency in seconds if successful and under timeout, else None.
    """
    test_url = "https://www.google.com/generate_204"
    proxies = {
        "all://": proxy_url
    }
    start_time = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxies=proxies, timeout=timeout_seconds) as client:
            response = await client.get(test_url)
            if response.status_code in (200, 204):
                return time.perf_counter() - start_time
    except Exception:
        pass
    return None


async def initialize_from_subscription() -> None:
    sub_url = os.environ.get("SUBSCRIPTION_URL", "").strip() or str(_runtime_state.get("subscription_url", "")).strip()
    if not sub_url:
        return

    print("INFO: Found SUBSCRIPTION_URL, initializing proxy node pool...")
    nodes = await _fetch_subscription(sub_url)
    exclude_keywords = ["流量", "重置", "到期", "剩余", "过期"]
    valid_nodes = [
        node for node in nodes
        if not any(word in str(node.get("name", "")) for word in exclude_keywords)
    ]
    valid_nodes = sorted(valid_nodes, key=_node_priority)
    _runtime_state["subscription_url"] = sub_url
    _runtime_state["node_pool"] = [
        {"raw_uri": node["raw_uri"], "name": node.get("name", node["raw_uri"][:80])}
        for node in valid_nodes
    ]
    print(f"INFO: Fetched {len(nodes)} nodes, {len(valid_nodes)} usable proxy nodes.")
    if valid_nodes:
        _runtime_state["node_pool_index"] = -1
        if await switch_next_node("initialization"):
            print("INFO: Node pool initialized with a low-latency node.")
        else:
            first = valid_nodes[0]
            try:
                proxy_url = _activate_node_by_uri(first["raw_uri"], first.get("name", ""), pool_index=0)
                _sync_proxy_env(proxy_url)
                print(f"INFO: Activated first fallback proxy node: {first.get('name', '')} -> {proxy_url}")
            except Exception as e:
                print(f"WARNING: Failed to activate first fallback node on initialization: {e}")


async def switch_next_node(reason: str = "") -> bool:
    pool: list[dict[str, Any]] = list(_runtime_state.get("node_pool") or [])
    if not pool:
        return False

    current = int(_runtime_state.get("node_pool_index", 0) or 0)
    
    # 1. First pass: Find a node with latency <= 800ms
    for offset in range(1, len(pool) + 1):
        next_index = (current + offset) % len(pool)
        node = pool[next_index]
        try:
            proxy_url = _activate_node_by_uri(
                str(node.get("raw_uri", "")),
                str(node.get("name", "")),
                pool_index=next_index,
            )
            
            latency = await test_proxy_latency(proxy_url, timeout_seconds=0.8)
            if latency is not None:
                _sync_proxy_env(proxy_url)
                msg = f"INFO: Switched proxy node to [{next_index + 1}/{len(pool)}] {node.get('name', '')} -> {proxy_url} (Latency: {int(latency * 1000)}ms)"
                if reason:
                    msg += f" ({reason})"
                print(msg)
                return True
            else:
                print(f"WARNING: Node [{next_index + 1}/{len(pool)}] {node.get('name', '')} failed latency test (>800ms or timeout/error). Trying next...")
        except Exception as exc:
            print(f"WARNING: Failed to activate node [{next_index + 1}/{len(pool)}] {node.get('name', '')}: {exc}")

    # 2. Second pass fallback: If all nodes failed the latency test, activate the first available one without latency constraints
    print("WARNING: All nodes in the pool failed the 800ms latency test. Falling back to the first available node without latency constraints.")
    for offset in range(1, len(pool) + 1):
        next_index = (current + offset) % len(pool)
        node = pool[next_index]
        try:
            proxy_url = _activate_node_by_uri(
                str(node.get("raw_uri", "")),
                str(node.get("name", "")),
                pool_index=next_index,
            )
            _sync_proxy_env(proxy_url)
            print(f"INFO: Fallback activation of node [{next_index + 1}/{len(pool)}] {node.get('name', '')} -> {proxy_url}")
            return True
        except Exception:
            pass

    return False


def is_rate_limit_error(exc_or_text: Any) -> bool:
    text = str(exc_or_text)
    upper = text.upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in upper or "TOO MANY REQUESTS" in upper


def is_transient_proxy_error(exc_or_text: Any) -> bool:
    text = str(exc_or_text)
    upper = text.upper()
    markers = [
        "TLS CONNECT ERROR",
        "OPENSSL_INTERNAL",
        "INVALID LIBRARY",
        "CONNECTIONRESETERROR",
        "CONNECTION RESET",
        "CLIENTOSERROR",
        "WINERROR 64",
        "FAILED TO PERFORM",
        "COULD NOT CONNECT",
        "PROXY",
        "TIMED OUT",
        "TIMEOUT",
        "HTTP/2 STREAM",
    ]
    return any(marker in upper for marker in markers)
