#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记 · 统一配置中心

设计目标：让「接入大模型」从「设环境变量 + 重启服务」变成「在网页设置里粘贴 Key 点保存」。

配置读取优先级（后者覆盖前者）：
    1. 内置默认值
    2. ~/.zhixue-notes/config.json（网页设置页写入，推荐方式）
    3. 环境变量（向后兼容老用户，也方便 Docker / 命令行使用）

对外主要接口：
    load_config()            读取合并后的完整配置 dict
    save_config(patch)       局部更新并落盘（只写用户改过的字段）
    provider_of(cfg=None)    解析出当前生效的 LLM 连接信息，没配就返回 None
    public_config()          给前端看的配置（API Key 打码）
    set_llm(provider, ...)   一行完成「选服务商 + 填 Key + 选模型」
    provider_catalog()       内置服务商预设（含免费额度说明、获取地址）
    models_for(provider)     某服务商的推荐模型列表

用法:
    from config import provider_of, load_config
    p = provider_of()          # -> {"base_url":..., "api_key":..., "model":...} 或 None
"""
import copy
import json
import os
import threading

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.expanduser("~"), ".zhixue-notes")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 服务商预设目录
# ---------------------------------------------------------------------------
# 说明：全部走 OpenAI 兼容协议（/chat/completions），所以一套代码通吃。
# 免费额度会变动，以官网为准；这里只负责「一键填好 base_url」。
PROVIDER_CATALOG = [
    {
        "id": "siliconflow",
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-14B-Instruct",
                   "THUDM/glm-4-9b-chat", "deepseek-ai/DeepSeek-V2.5"],
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "note": "国内直连，注册送免费额度，部分小模型长期免费。新手首选。",
        "free": True,
    },
    {
        "id": "ollama",
        "name": "Ollama（本地模型，完全免费）",
        "base_url": "http://127.0.0.1:11434/v1",
        "key_url": "https://ollama.com/download",
        "models": ["qwen2.5:7b", "qwen2.5:3b", "llama3.1:8b", "gemma2:9b"],
        "default_model": "qwen2.5:7b",
        "note": "装好 Ollama 后执行 ollama pull qwen2.5:7b 即可。数据不出本机、无需 Key、无额度限制。",
        "free": True,
        "no_key": True,
    },
    {
        "id": "modelscope",
        "name": "魔搭 ModelScope",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "key_url": "https://modelscope.cn/my/myaccesstoken",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "note": "阿里达摩院社区，绑定阿里云账号后有每日免费调用额度。",
        "free": True,
    },
    {
        "id": "dashscope",
        "name": "阿里云百炼（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_url": "https://bailian.console.aliyun.com/",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen2.5-7b-instruct"],
        "default_model": "qwen-plus",
        "note": "新用户有免费额度，qwen-turbo 便宜快速。老版本用的是这个。",
        "free": True,
    },
    {
        "id": "deepseek",
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "key_url": "https://platform.deepseek.com/api_keys",
        "models": ["deepseek-chat"],
        "default_model": "deepseek-chat",
        "note": "中文理解与总结质量很好，价格低，适合做笔记提炼。",
        "free": False,
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus"],
        "default_model": "glm-4-flash",
        "note": "glm-4-flash 为免费档模型，中文教学场景表现不错。",
        "free": True,
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/keys",
        "models": ["qwen/qwen-2.5-7b-instruct:free",
                   "meta-llama/llama-3.1-8b-instruct:free",
                   "deepseek/deepseek-chat"],
        "default_model": "qwen/qwen-2.5-7b-instruct:free",
        "note": "带 :free 后缀的模型免费，但有速率限制；需能访问境外网络。",
        "free": True,
    },
    {
        "id": "openai",
        "name": "OpenAI 官方",
        "base_url": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "default_model": "gpt-4o-mini",
        "note": "需境外网络与付费账号。老版本支持。",
        "free": False,
    },
    {
        "id": "custom",
        "name": "自定义（任意 OpenAI 兼容接口）",
        "base_url": "",
        "key_url": "",
        "models": [],
        "default_model": "",
        "note": "任何提供 /v1/chat/completions 的服务都能接：填 Base URL、Key、模型名即可。",
        "free": False,
    },
]

_CATALOG_BY_ID = {p["id"]: p for p in PROVIDER_CATALOG}

# 内置默认配置
DEFAULTS = {
    "llm": {
        "enabled": False,
        "provider": "siliconflow",
        "base_url": _CATALOG_BY_ID["siliconflow"]["base_url"],
        "api_key": "",
        "model": _CATALOG_BY_ID["siliconflow"]["default_model"],
        "timeout": 60,
        "max_retries": 2,
        # 提炼强度：fast=只做分章+要点；standard=分章+要点+通俗解释；deep=额外生成考点与自测题
        "quality": "standard",
    },
    "transcribe": {
        "model": "small",          # tiny / base / small / medium / large-v3
        "compute_type": "int8",
        "language": "zh",
    },
    "extract": {
        "max_chapters": 12,        # 最多分多少章
        "points_per_chapter": 5,   # 每章最多几个要点
        "min_sentence_len": 6,
    },
    "network": {
        "reader_fallback": True,   # 网页抓不到时，允许调用 r.jina.ai 公共阅读器兜底
        "timeout": 20,
    },
    "cleanup": {
        # 任务数据清理策略（旧版从不清理，长期使用会占满磁盘、内存里 _tasks 也会一直涨）
        "keep_recent": 50,         # 最多保留多少个已完成任务（按完成时间从新到旧）
        "max_age_days": 30,        # 超过多少天的任务自动删除
        "max_total_mb": 3000,      # 任务目录总占用上限（MB），超出时从最旧的开始删
    },
    "output": {
        "fmts": ["md", "pdf", "mindmap"],
        "obsidian_vault": "",
    },
}

# 环境变量映射（向后兼容）
#   config 路径 -> 环境变量名（按顺序取第一个存在的）
ENV_MAP = {
    "llm.api_key": ["ZHIXUE_LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"],
    "llm.base_url": ["ZHIXUE_LLM_BASE_URL", "OPENAI_BASE_URL"],
    "llm.model": ["ZHIXUE_LLM_MODEL"],
}


def _deep_merge(base, override):
    """
    递归合并：override 里有的键覆盖 base，dict 递归、其他类型直接替换。

    关键：必须 deepcopy，不能用 dict(base) 浅拷贝。
    浅拷贝会让返回值的嵌套 dict 与 DEFAULTS 共享同一个对象，
    调用方一改返回值（哪怕只是 cfg["llm"]["timeout"] = 90），
    DEFAULTS 就被永久污染，后续所有读取都会带上这个值 —— 配置会串味。
    """
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_disk():
    """读取 config.json；不存在或损坏时返回 {}"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_config():
    """返回合并后的完整配置（默认值 < 磁盘 < 环境变量）"""
    disk = _read_disk()
    cfg = _deep_merge(DEFAULTS, disk)

    # 环境变量兜底：只在用户没有在 config.json 里配置该项时生效
    for dotted, env_names in ENV_MAP.items():
        section, key = dotted.split(".", 1)
        if (disk.get(section) or {}).get(key):
            continue          # 磁盘里已配置，环境变量不覆盖
        for env_name in env_names:
            val = os.environ.get(env_name)
            if val:
                cfg.setdefault(section, {})[key] = val
                break

    # 有 Key 就自动启用，避免用户困惑「明明配了 Key 却没生效」。
    # 但用户如果显式写过 enabled=false，必须尊重，否则关不掉。
    user_set_enabled = "enabled" in (disk.get("llm") or {})
    if cfg["llm"].get("api_key") and not user_set_enabled:
        cfg["llm"]["enabled"] = True
    return cfg


def save_config(patch):
    """
    局部更新配置并落盘。
    patch 形如 {"llm": {"api_key": "sk-xxx"}}，只覆盖传进来的字段。
    返回合并后的完整配置。
    """
    with _lock:
        disk = _read_disk()
        merged = _deep_merge(disk, patch or {})
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)      # 原子替换，避免写一半损坏
    return load_config()


def reset_config():
    """删除配置文件，回到默认状态"""
    with _lock:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
    return load_config()


def provider_of(cfg=None):
    """
    解析当前生效的 LLM 连接信息。
    返回 {"provider","base_url","api_key","model","timeout","max_retries","quality"}，
    未启用或缺少必要字段时返回 None。
    """
    cfg = cfg or load_config()
    llm = cfg.get("llm") or {}
    if not llm.get("enabled"):
        return None
    base_url = (llm.get("base_url") or "").rstrip("/")
    model = (llm.get("model") or "").strip()
    if not base_url or not model:
        return None
    # 本地 Ollama 不需要 Key；其他服务商必须有 Key
    is_local = ("127.0.0.1" in base_url) or ("localhost" in base_url)
    api_key = (llm.get("api_key") or "").strip()
    if not api_key and not is_local:
        return None
    try:
        timeout = int(llm.get("timeout") or 60)
    except Exception:
        timeout = 60
    try:
        max_retries = int(llm.get("max_retries") or 2)
    except Exception:
        max_retries = 2
    return {
        "provider": llm.get("provider") or "custom",
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout": max(5, min(300, timeout)),
        "max_retries": max(0, min(5, max_retries)),
        "quality": llm.get("quality") or "standard",
        "is_local": is_local,
    }


def _mask(key):
    """API Key 打码：sk-abc...wxyz"""
    if not key:
        return ""
    key = str(key)
    if len(key) <= 10:
        return key[:2] + "***"
    return key[:6] + "..." + key[-4:]


def public_config():
    """
    给前端用的配置视图：API Key 打码，附带当前生效状态。
    前端据此渲染设置页（显示「已连接 / 未配置」）。
    """
    cfg = load_config()
    llm = dict(cfg.get("llm") or {})
    raw_key = llm.get("api_key") or ""
    llm["api_key"] = _mask(raw_key)
    llm["api_key_set"] = bool(raw_key)
    llm["active"] = provider_of(cfg) is not None
    cfg = dict(cfg)
    cfg["llm"] = llm
    cfg["_provider_catalog"] = PROVIDER_CATALOG
    cfg["_config_path"] = CONFIG_PATH
    return cfg


def set_llm(provider=None, api_key=None, model=None,
            base_url=None, enabled=True, quality=None):
    """
    一行完成大模型配置（设置页「保存并测试」就是调它）。
    provider 传内置 id（siliconflow / ollama / ...）时会自动填好 base_url 与默认模型。
    """
    patch = {"llm": {}}
    if enabled is not None:
        patch["llm"]["enabled"] = bool(enabled)
    if quality:
        patch["llm"]["quality"] = quality

    if provider:
        preset = _CATALOG_BY_ID.get(provider)
        patch["llm"]["provider"] = provider
        if preset:
            # 只有用户没自己填 base_url 时才用预设
            if not base_url and preset.get("base_url"):
                patch["llm"]["base_url"] = preset["base_url"]
            if not model and preset.get("default_model"):
                patch["llm"]["model"] = preset["default_model"]

    if base_url:
        patch["llm"]["base_url"] = base_url.rstrip("/")
    if model:
        patch["llm"]["model"] = model.strip()
    if api_key is not None:
        patch["llm"]["api_key"] = api_key.strip()

    return save_config(patch)


def provider_catalog():
    return PROVIDER_CATALOG


def models_for(provider_id):
    preset = _CATALOG_BY_ID.get(provider_id)
    return (preset or {}).get("models") or []


if __name__ == "__main__":
    # 自测：打印当前配置与生效状态
    import pprint
    print("配置文件:", CONFIG_PATH, "存在:", os.path.exists(CONFIG_PATH))
    pprint.pprint(load_config())
    print("\n生效的 LLM 连接:")
    pprint.pprint(provider_of())
    print("\n内置服务商:", [p["id"] for p in PROVIDER_CATALOG])
