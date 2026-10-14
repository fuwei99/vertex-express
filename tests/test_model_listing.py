"""直接跑真实的 models_api.list_models()，验证「不自动加前后缀」的新行为。

做法：把 fastapi / auth / model_loader / credentials_manager 这些重依赖 stub 掉，
只为了能把真实的 models_api 模块导入进来。被测的是真代码。

    python3 tests/test_model_listing.py
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# models_api 在 app/routes/ 下，它又 import app/ 下的 config、gemini_native_models
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "routes"))


def stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Router:
    def _deco(self, *a, **k):
        def deco(fn):
            return fn
        return deco
    get = post = put = delete = _deco


stub("fastapi", APIRouter=lambda: _Router(), Depends=lambda dep: dep,
     Request=object, HTTPException=type("HTTPException", (Exception,), {}))
stub("auth", get_api_key=lambda: "test-key")
stub("credentials_manager", CredentialManager=type("CredentialManager", (), {}))

# 上游模型清单：模拟 vertexModels.json 里的基础模型
UPSTREAM = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash-lite"]


async def _refresh():
    return None


async def _vertex():
    return list(UPSTREAM)


async def _express():
    return list(UPSTREAM)


stub("model_loader", get_vertex_models=_vertex, get_vertex_express_models=_express,
     refresh_models_config_cache=_refresh)

import models_api  # noqa: E402

FAILED = []


def check(label, actual, expected):
    if actual != expected:
        FAILED.append(f"{label}\n    期望: {expected!r}\n    实际: {actual!r}")
    else:
        print(f"  ok  {label}")


class _State:
    credential_manager = type("C", (), {"get_total_credentials": lambda self: 0})()
    express_key_manager = type("E", (), {"get_total_keys": lambda self: 1})()


class _Request:
    app = type("App", (), {"state": _State()})()


def list_ids():
    result = asyncio.run(models_api.list_models(_Request(), api_key="test-key"))
    return [item["id"] for item in result["data"]]


print("== 1. 默认（开关关）：不再自动加前后缀 ==")
ids = list_ids()
for bad in ("[EXPRESS] gemini-3.7-flash", "[PAY] gemini-3.7-flash",
            "gemini-3.7-flash-encrypt", "gemini-3.7-flash-search", "gemini-3.7-flash-auto",
            "gemini-3.7-flash-openai", "gemini-3.7-flash-nothinking", "gemini-3.7-flash-max"):
    check(f"不包含 {bad!r}", bad in ids, False)
check("基础模型都在", all(m in ids for m in UPSTREAM), True)

print("== 2. 模型数量不再膨胀 ==")
check("只有 3 个基础模型 + models.json 条目 + 映射（远小于 140）", len(ids) < 30, True)
print(f"     实际返回 {len(ids)} 个：{sorted(ids)}")

print("== 3. models.json 里已写好的思考/图片等级条目会被列出 ==")
native_ids = [m["id"] for m in models_api.get_gemini_native_models()]
check("models.json 条目数 > 0", len(native_ids) > 0, True)
missing = [m for m in native_ids if m not in ids]
check("全部出现在 /v1/models 里", missing, [])
print(f"     其中思考/图片等级样例：{sorted(native_ids)[:6]}")

print("== 4. 自己注册的映射会出现 ==")
check("_registered_variants() 能读到 alias_map 结构",
      isinstance(models_api._registered_variants(), dict), True)

print("== 5. 用真实模型清单对比：旧行为 vs 新行为 ==")
import json  # noqa: E402
import os  # noqa: E402

_real = json.loads((ROOT / "vertexModels.json").read_text(encoding="utf-8"))
REAL_MODELS = _real.get("vertex_express_models", [])
print(f"     vertexModels.json 里的基础模型：{len(REAL_MODELS)} 个")


async def _real_vertex():
    return list(REAL_MODELS)


async def _real_express():
    return list(REAL_MODELS)


models_api.get_vertex_models = _real_vertex
models_api.get_vertex_express_models = _real_express

new_ids = list_ids()
os.environ["EXPAND_MODEL_VARIANTS"] = "true"
try:
    legacy_ids = list_ids()
finally:
    os.environ.pop("EXPAND_MODEL_VARIANTS", None)

print(f"     新行为（默认）：{len(new_ids)} 个")
print(f"     旧行为（开关开）：{len(legacy_ids)} 个")
check("旧行为确实膨胀到 100+（这就是你看到的 140 多个）", len(legacy_ids) > 100, True)
check("新行为回到几十个以内", len(new_ids) < 60, True)
check("旧行为里有 [EXPRESS] 前缀", any(m.startswith("[EXPRESS] ") for m in legacy_ids), True)
check("新行为里没有任何前缀", any(m.startswith("[") for m in new_ids), False)
check("新行为里没有任何自动后缀",
      [m for m in new_ids if m.endswith(("-encrypt", "-encrypt-full", "-auto", "-search",
                                         "-openai", "-openaisearch"))], [])
check("新行为保留了思考等级条目", any(m.endswith("-low") for m in new_ids), True)
check("新行为保留了图片等级条目", any(m.endswith("-2K") for m in new_ids), True)

if FAILED:
    print("\n失败 %d 项：" % len(FAILED))
    for item in FAILED:
        print("  FAIL " + item)
    sys.exit(1)
print("\n全部通过")
