# -*- coding: utf-8 -*-
"""config.py 全套自测：保存/读取/环境变量优先级/打码/provider_of 判定"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BACKEND = os.path.join(REPO, "backend")
sys.path.insert(0, BACKEND)

# 工作目录：默认 dev/_work（已 gitignore），可用 ZHIXUE_TEST_WORK 覆盖
WORK = os.environ.get("ZHIXUE_TEST_WORK") or os.path.join(HERE, "_work")
tmp = os.path.join(WORK, "cfgtest")
os.makedirs(tmp, exist_ok=True)

import config as C

# 把配置目录指向可写位置（避免污染真实用户目录）
C.DATA_DIR = tmp
C.CONFIG_PATH = os.path.join(tmp, "config.json")
if os.path.exists(C.CONFIG_PATH):
    os.remove(C.CONFIG_PATH)

fails = []
total = [0]


def check(name, cond, extra=""):
    total[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


print("1) 初始状态：未配置 -> provider_of 应为 None")
check("provider_of() is None", C.provider_of() is None)

print("\n2) set_llm 选硅基流动 + 填 Key")
cfg = C.set_llm(provider="siliconflow", api_key="sk-abcdefghijklmnopqrstuvwxyz")
p = C.provider_of()
check("base_url 来自预设", p and p["base_url"] == "https://api.siliconflow.cn/v1", p)
check("model 来自预设", p and p["model"] == "Qwen/Qwen2.5-7B-Instruct", p)
check("api_key 正确", p and p["api_key"] == "sk-abcdefghijklmnopqrstuvwxyz", p)
check("落盘成功", os.path.exists(C.CONFIG_PATH))
check("磁盘只存用户改过的字段", "transcribe" not in json.load(open(C.CONFIG_PATH, encoding="utf-8")))

print("\n3) 切换到 Ollama（本地，无 Key 也应可用）")
C.set_llm(provider="ollama", api_key="")
p = C.provider_of()
check("ollama base_url", p and p["base_url"] == "http://127.0.0.1:11434/v1", p)
check("无 Key 也判定为可用", p is not None, p)
check("标记为本地", p and p["is_local"] is True, p)

print("\n4) 切回需要 Key 的服务商但 Key 为空 -> 应判定不可用")
C.set_llm(provider="deepseek", api_key="")
check("缺 Key -> None", C.provider_of() is None)

print("\n5) public_config 打码")
C.set_llm(provider="deepseek", api_key="sk-1234567890abcdefghij")
pc = C.public_config()
check("打码格式", pc["llm"]["api_key"] == "sk-123...ghij", pc["llm"]["api_key"])
check("api_key_set 为 True", pc["llm"]["api_key_set"] is True)
check("active 为 True", pc["llm"]["active"] is True)
check("catalog 随配置返回", len(pc["_provider_catalog"]) >= 8)

print("\n6) enabled=False 时不生效")
C.save_config({"llm": {"enabled": False}})
check("禁用后 None", C.provider_of() is None)

print("\n7) 环境变量兜底（磁盘未配置该项时生效）")
C.reset_config()
os.environ["DASHSCOPE_API_KEY"] = "sk-envtest000000000000"
p = C.provider_of()
check("env Key 被采纳", p and p["api_key"] == "sk-envtest000000000000", p)
check("env 有 Key 时自动 enabled", C.load_config()["llm"]["enabled"] is True)

print("\n8) 磁盘配置优先于环境变量")
C.set_llm(provider="siliconflow", api_key="sk-diskwins0000000000")
p = C.provider_of()
check("磁盘 Key 覆盖 env", p and p["api_key"] == "sk-diskwins0000000000", p)

print("\n9) quality / timeout 边界")
C.save_config({"llm": {"timeout": 99999, "max_retries": 99}})
p = C.provider_of()
check("timeout 被夹到 300", p and p["timeout"] == 300, p)
check("max_retries 被夹到 5", p and p["max_retries"] == 5, p)

print("\n10) 损坏的配置文件不致崩溃")
with open(C.CONFIG_PATH, "w", encoding="utf-8") as f:
    f.write("{ this is not json ")
cfg = C.load_config()
check("损坏后回落到默认值", cfg["llm"]["provider"] == "siliconflow", cfg["llm"]["provider"])
# 注意：第 7 步设过 DASHSCOPE_API_KEY，此处它会正常接管，
# 所以要清掉所有 LLM 相关环境变量，才能验证「无凭据 -> None」这一条。
_saved = {k: os.environ.pop(k) for k in
          ("ZHIXUE_LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY") if k in os.environ}
check("损坏且无 env 兜底 -> None", C.provider_of() is None, C.provider_of())
os.environ.update(_saved)
check("损坏但有 env 兜底 -> 仍可用（环境变量优先于损坏文件）",
      C.provider_of() is not None)

print("\n" + "=" * 46)
print("结果：%d 项检查，%d 项通过，%d 项失败" % (total[0], total[0] - len(fails), len(fails)))
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("config.py 全部自测通过")

