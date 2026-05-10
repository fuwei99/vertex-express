import os
from typing import Any

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
    _runtime_state["node_pool_index"] = 0
    print(f"INFO: Fetched {len(nodes)} nodes, {len(valid_nodes)} usable proxy nodes.")
    if valid_nodes:
        first = valid_nodes[0]
        proxy_url = _activate_node_by_uri(first["raw_uri"], first.get("name", ""), pool_index=0)
        _sync_proxy_env(proxy_url)
        print(f"INFO: Activated first proxy node: {first.get('name', '')} -> {proxy_url}")


async def switch_next_node(reason: str = "") -> bool:
    pool: list[dict[str, Any]] = list(_runtime_state.get("node_pool") or [])
    if not pool:
        return False

    current = int(_runtime_state.get("node_pool_index", 0) or 0)
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
            msg = f"INFO: Switched proxy node to [{next_index + 1}/{len(pool)}] {node.get('name', '')} -> {proxy_url}"
            if reason:
                msg += f" ({reason})"
            print(msg)
            return True
        except Exception as exc:
            print(f"WARNING: Failed to activate node [{next_index + 1}/{len(pool)}] {node.get('name', '')}: {exc}")
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
