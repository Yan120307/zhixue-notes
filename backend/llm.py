#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记 LLM 模块 — 大模型增强（通义千问 / OpenAI 兼容）

支持两种 Provider（自动探测环境变量）：
  1. DASHSCOPE_API_KEY  → 通义千问（阿里云百炼）
     https://dashscope.aliyuncs.com/compatible-mode/v1
  2. OPENAI_API_KEY     → OpenAI 官方
     https://api.openai.com/v1

未配置任何 Key 时返回 None，上层流程自动降级为基础提炼。

用法:
    from llm import llm_available, llm_enhance_note, llm_chat
"""
import json
import os
import urllib.request
import urllib.error


def _get_provider():
    """探测可用的 LLM Provider"""
    if os.environ.get("DASHSCOPE_API_KEY"):
        return {
            "name": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": os.environ["DASHSCOPE_API_KEY"],
            "model": os.environ.get("ZHIXUE_LLM_MODEL", "qwen-plus"),
        }
    if os.environ.get("OPENAI_API_KEY"):
        return {
            "name": "openai",
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "api_key": os.environ["OPENAI_API_KEY"],
            "model": os.environ.get("ZHIXUE_LLM_MODEL", "gpt-4o-mini"),
        }
    return None


def llm_available():
    """检查是否有可用的大模型"""
    return _get_provider() is not None


def llm_chat(messages, max_tokens=2000, temperature=0.7):
    """调用 OpenAI 兼容接口，返回文本或 None"""
    provider = _get_provider()
    if not provider:
        return None
    url = provider["base_url"] + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + provider["api_key"],
    }
    body = json.dumps({
        "model": provider["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def llm_enhance_note(kmap, transcript_excerpt="", source_url=""):
    """
    用大模型增强笔记：通俗解释 + 一句话掌握 + 深度补充建议

    参数:
        kmap: knowledge_map dict
        transcript_excerpt: 字幕/文稿摘录（前 3000 字）
        source_url: 资源链接

    返回:
        增强后的 kmap dict（每个知识点增加 plain_explain / one_liner 字段），
        或 None（无可用模型时）
    """
    if not llm_available():
        return None

    points = []
    for ch in kmap.get("chapters", []):
        for p in ch.get("points", []):
            points.append({"chapter": ch["title"], "point": p["title"],
                           "summary": p.get("summary", "")})
    if not points:
        return None

    prompt = """你是一位善于深入浅出的学习笔记专家。请对以下知识点逐一给出通俗解释。

要求：
1. 每个知识点输出两行：
   - 一句话掌握：用一句大白话概括（不超过30字）
   - 通俗解释：用生活化类比/大白话讲明白原理，面向零基础学习者，100字以内
2. 输出 JSON 数组格式，每个元素：
   {"point": "知识点标题", "one_liner": "...", "plain_explain": "..."}

知识点列表：
%s

%s

只输出 JSON 数组，不要其他内容。""" % (
        json.dumps(points, ensure_ascii=False, indent=1),
        ("参考资料摘录：" + transcript_excerpt[:3000]) if transcript_excerpt else "")

    result = llm_chat([
        {"role": "system", "content": "你是一位善于深入浅出的学习笔记专家，输出必须是纯 JSON。"},
        {"role": "user", "content": prompt},
    ], max_tokens=3000)

    if not result:
        return None

    # 尝试解析 JSON（容错：可能被包裹在 markdown 代码块中）
    text = result.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        enhancements = json.loads(text)
        if not isinstance(enhancements, list):
            return None
    except (json.JSONDecodeError, ValueError):
        return None

    # 按 point 标题匹配合并回 kmap
    by_title = {e.get("point", ""): e for e in enhancements if isinstance(e, dict)}
    for ch in kmap.get("chapters", []):
        for p in ch.get("points", []):
            e = by_title.get(p["title"])
            if e:
                p["one_liner"] = e.get("one_liner", "")
                p["plain_explain"] = e.get("plain_explain", "")
    kmap["llm_enhanced"] = True
    return kmap


def llm_generate_summary(transcript_excerpt, source_url=""):
    """
    用大模型生成课程概述与考点清单

    返回: {"overview": "...", "exam_points": ["...", ...]} 或 None
    """
    if not llm_available() or not transcript_excerpt:
        return None

    prompt = """基于以下学习材料内容，输出：
1. overview: 2-3句话概括核心内容与价值
2. exam_points: 5-8个必背考点（每个一句话）

输出 JSON：{"overview": "...", "exam_points": ["...", ...]}

材料：
%s

只输出 JSON，不要其他内容。""" % transcript_excerpt[:4000]

    result = llm_chat([
        {"role": "system", "content": "你是学习笔记专家，输出必须是纯 JSON。"},
        {"role": "user", "content": prompt},
    ], max_tokens=1500)

    if not result:
        return None
    text = result.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "overview" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


if __name__ == "__main__":
    # 快速自测
    print("LLM 可用:", llm_available())
    if llm_available():
        r = llm_chat([{"role": "user", "content": "回复两个字：你好"}], max_tokens=20)
        print("连通测试:", repr(r))