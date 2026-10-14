"""region_rules 的独立测试。不依赖 fastapi/httpx，直接跑：

    python3 tests/test_region_rules.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from region_rules import (  # noqa: E402
    build_model_url, location_plan, normalize_url_style, resolve_location,
)

FAILED = []


def check(label, actual, expected):
    if actual != expected:
        FAILED.append(f"{label}\n    期望: {expected!r}\n    实际: {actual!r}")
    else:
        print(f"  ok  {label}")


print("== 1. 旧字段 location 仍然优先 ==")
check("显式 location 覆盖运行时",
      resolve_location({"location": "us-central1"}, "global"), "us-central1")
check("显式 location 忽略排除集",
      resolve_location({"location": "global", "excludeLocations": ["global"]}, "global"), "global")

print("== 2. 白名单（绑定区域） ==")
check("白名单取第一个",
      resolve_location({"locations": ["us", "eu"]}, "global"), "us")
check("白名单跳过被排除项",
      resolve_location({"locations": ["global", "eu"], "excludeLocations": ["global"]}, "global"), "eu")

print("== 3. 无约束时用运行时区域 ==")
check("无约束 → 运行时区域",
      resolve_location({}, "asia-northeast1"), "asia-northeast1")
check("模型为 None → 运行时区域",
      resolve_location(None, "europe-west4"), "europe-west4")

print("== 4. 核心场景：运行时是 global，但模型排除 global ==")
plan = location_plan({"excludeLocations": ["global"]}, "global")
check("改选其他区域（不再用 global）", plan["location"], "us")
check("来源标记为 fallback", plan["source"], "fallback")
check("排除集被记录", plan["excluded"], ["global"])

check("排除 global 与 us → 落到 eu",
      resolve_location({"excludeLocations": ["global", "us"]}, "global"), "eu")
check("排除 global/us/eu → 只能回 conflict 保留原值",
      resolve_location({"excludeLocations": ["global", "us", "eu"]}, "global"), "global")

print("== 5. 运行时区域没被排除时不动它 ==")
check("运行时 us-central1 未被排除 → 保持",
      resolve_location({"excludeLocations": ["global"]}, "us-central1"), "us-central1")

print("== 6. url_style ==")
check("非法值回落 long", normalize_url_style("weird"), "long")
check("空值回落 long", normalize_url_style(None), "long")
check("大小写不敏感", normalize_url_style("SHORT"), "short")

long_url = build_model_url("proj-1", "us", "gemini-4-argon", "streamGenerateContent",
                           stream=True, api_key="AQ.test", style="long")
check("长链接形状", long_url,
      "https://aiplatform.googleapis.com/v1/projects/proj-1/locations/us"
      "/publishers/google/models/gemini-4-argon:streamGenerateContent?alt=sse&key=AQ.test")

short_url = build_model_url("proj-1", "us", "gemini-4-argon", "generateContent",
                            api_key="AQ.test", style="short")
check("短链接形状（路径里无项目/区域）", short_url,
      "https://aiplatform.googleapis.com/v1/publishers/google/models/"
      "gemini-4-argon:generateContent?key=AQ.test")

check("长链接默认（不传 style）",
      build_model_url("p", "global", "m", "generateContent"),
      "https://aiplatform.googleapis.com/v1/projects/p/locations/global"
      "/publishers/google/models/m:generateContent")

print("== 7. 项目 ID 缺失时不应拼出半截路径 ==")
check("空项目 ID 仍然产出结构完整的 URL（由调用方决定是否降级为短链接）",
      build_model_url("", "global", "m", "generateContent"),
      "https://aiplatform.googleapis.com/v1/projects//locations/global"
      "/publishers/google/models/m:generateContent")

if FAILED:
    print("\n失败 %d 项：" % len(FAILED))
    for item in FAILED:
        print("  FAIL " + item)
    sys.exit(1)
print("\n全部通过")
