"""Vertex 端点选择规则：区域约束与 URL 形态。

设计原则（按需求）：**代码里不含任何模型专属的区域硬编码**。
每个模型的区域约束写在 `models.json` 的模型条目里，支持三种写法：

    "location": "us"                  # 钉定单个区域（旧字段，继续兼容）
    "locations": ["us", "eu"]         # 白名单 = 绑定到这几个区域
    "excludeLocations": ["global"]    # 黑名单 = 排除这些区域

解析顺序：
    1. 模型自己写了 `location` → 直接用（旧行为不变）
    2. 模型写了 `locations` 白名单 → 取第一个不在 `excludeLocations` 里的
    3. 否则用运行时区域（可能已被「按节点自动切换」改过）
    4. 运行时区域被该模型排除 → 按 FALLBACK_LOCATIONS 顺序挑一个没被排除的

URL 形态（`url_style`）：
    long（默认）: https://aiplatform.googleapis.com/v1/projects/{p}/locations/{loc}/publishers/google/models/{m}:{action}
    short       : https://aiplatform.googleapis.com/v1/publishers/google/models/{m}:{action}

两种都挂在**全局主机**上，区域只写在路径里。这是另外两个参考项目的实测结论：
Express Key 下 `https://{location}-aiplatform.googleapis.com/...` 这种 dashed 区域主机不通，
所以钉定区域靠「全局主机 + 路径带 location」，而不是换主机。
"""

LONG = "long"
SHORT = "short"
URL_STYLES = (LONG, SHORT)
DEFAULT_URL_STYLE = LONG

# 全局主机。区域通过路径表达，不换主机（见模块 docstring）。
GLOBAL_HOST = "aiplatform.googleapis.com"

# 运行时区域被模型排除、且模型没给白名单时的候选顺序。
# 不是模型专属规则，只是 Vertex 的通用入口：两个多区域 + global。
FALLBACK_LOCATIONS = ("us", "eu", "global")


def normalize_url_style(value, default: str = DEFAULT_URL_STYLE) -> str:
    """把外部输入收敛成合法的 url_style；非法值回落到默认。"""
    text = str(value or "").strip().lower()
    return text if text in URL_STYLES else default


def _as_location_list(value) -> list[str]:
    """接受字符串或字符串列表，去空去重，保持顺序。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def location_plan(model_config: dict | None, runtime_location: str = "global") -> dict:
    """算出该模型这次实际要用的区域，并说明为什么。

    返回 {"location": str, "source": str, "excluded": [...], "allowed": [...]}。
    `source` 便于日志与排错：pin / whitelist / runtime / fallback。
    """
    config = model_config if isinstance(model_config, dict) else {}
    runtime = str(runtime_location or "").strip() or "global"
    excluded = _as_location_list(config.get("excludeLocations"))
    allowed = _as_location_list(config.get("locations"))

    # 1. 旧字段：显式钉定，优先级最高，不做排除判断（用户明确要求就用它）。
    pinned = str(config.get("location") or "").strip()
    if pinned:
        return {"location": pinned, "source": "pin", "excluded": excluded, "allowed": allowed}

    # 2. 白名单：绑定式。取第一个没被排除的。
    if allowed:
        for candidate in allowed:
            if candidate not in excluded:
                return {"location": candidate, "source": "whitelist",
                        "excluded": excluded, "allowed": allowed}

    # 3. 运行时区域可用就用它。
    if runtime not in excluded:
        return {"location": runtime, "source": "runtime", "excluded": excluded, "allowed": allowed}

    # 4. 运行时区域被模型排除 → 另选一个允许的区域。
    for candidate in FALLBACK_LOCATIONS:
        if candidate not in excluded:
            return {"location": candidate, "source": "fallback",
                    "excluded": excluded, "allowed": allowed}

    # 白名单与兜底全被排除（配置矛盾）：保留运行时区域，不要静默换掉。
    return {"location": runtime, "source": "conflict", "excluded": excluded, "allowed": allowed}


def resolve_location(model_config: dict | None, runtime_location: str = "global") -> str:
    """只要区域值的简写。"""
    return location_plan(model_config, runtime_location)["location"]


def build_model_url(project_id: str, location: str, model: str, action: str,
                    stream: bool = False, api_key: str | None = None,
                    style: str = DEFAULT_URL_STYLE) -> str:
    """按形态拼出上游 URL。

    long  : 项目路径式，区域写在 locations/{loc}
    short : Express 文档式，路径里没有项目与区域
    """
    model = str(model or "").strip()
    location = str(location or "").strip() or "global"
    action = str(action or "generateContent").strip() or "generateContent"

    if normalize_url_style(style) == SHORT:
        path = f"/v1/publishers/google/models/{model}:{action}"
    else:
        path = (f"/v1/projects/{str(project_id or '').strip()}"
                f"/locations/{location}/publishers/google/models/{model}:{action}")

    url = f"https://{GLOBAL_HOST}{path}"

    params: list[str] = []
    if stream:
        params.append("alt=sse")
    if api_key:
        params.append(f"key={api_key}")
    if params:
        url += "?" + "&".join(params)
    return url


def describe_plan(plan: dict, model_id: str = "") -> str:
    """一行日志，便于核对某次请求到底用了哪个区域、为什么。"""
    label = f"模型={model_id} " if model_id else ""
    parts = [f"{label}区域={plan['location']} 来源={plan['source']}"]
    if plan.get("allowed"):
        parts.append("白名单=" + ",".join(plan["allowed"]))
    if plan.get("excluded"):
        parts.append("排除=" + ",".join(plan["excluded"]))
    return "[区域规则] " + " ".join(parts)
