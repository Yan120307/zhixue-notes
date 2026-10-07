#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记 · 大模型模块

相比旧版的四个关键改进：
  1. 配置来自 config.py（网页设置页可改），不再只认环境变量 —— 配置不再麻烦
  2. 分块处理长文稿 + 逐块提炼，解决「文稿一长就瞎总结」（旧版只截前 3000/4000 字）
  3. 知识点的标题/摘要由大模型生成，不再用「截断的句子」当标题 —— 解决「乱总结」
  4. 带指数退避重试、JSON 容错解析、并发提取，单点失败不会让整份笔记退化

对外接口：
    llm_available()                          是否已配置可用
    provider_info()                          当前连接信息（不含 Key 全文）
    test_connection()                        连通性测试，给设置页用
    llm_chat(messages, ...)                  底层对话
    llm_plan_chapters(chunks)                规划章节结构
    llm_extract_points(chunk, ...)           从一段文稿提炼知识点
    llm_enhance_note(kmap, ...)              按章节补「一句话掌握 + 通俗解释」
    llm_generate_summary(full_text, ...)     概述 + 必背考点
    llm_build_knowledge_map(items, ...)      一站式：文稿 -> 完整知识图谱
"""
import json
import re
import time
import urllib.error
import urllib.request

try:
    from concurrent.futures import ThreadPoolExecutor
except ImportError:                       # pragma: no cover
    ThreadPoolExecutor = None

try:
    from config import provider_of, load_config
except ImportError:                       # 允许单独运行本文件调试
    def provider_of(cfg=None):
        return None

    def load_config():
        return {"extract": {"max_chapters": 12, "points_per_chapter": 5}}


# ---------------------------------------------------------------------------
# 基础：连接与调用
# ---------------------------------------------------------------------------
# 单次请求发送的最大字符数。中文 1 字约 1~1.5 token，4000 字约 4000~6000 token，
# 加上 prompt 与输出仍能安全落在常见 8K~32K 上下文窗口内。
CHUNK_CHARS = 4000
# 送去做「全局概述 / 考点」的文稿上限（这部分只要总览，不需要全文）
SUMMARY_CHARS = 12000


def llm_available():
    """是否已配置可用的大模型"""
    return provider_of() is not None


def provider_info():
    """当前连接信息（给设置页显示，不含 Key 全文）"""
    p = provider_of()
    if not p:
        return {"active": False}
    return {
        "active": True,
        "provider": p["provider"],
        "base_url": p["base_url"],
        "model": p["model"],
        "is_local": p["is_local"],
        "quality": p["quality"],
    }


def llm_chat(messages, max_tokens=4000, temperature=0.3, timeout=None,
             json_mode=True, retries=None):
    """
    调用 OpenAI 兼容 /chat/completions。

    - temperature 默认 0.3：做笔记提炼要稳定、少发挥，不像闲聊需要 0.7
    - json_mode=True 时请求 response_format=json_object，让模型少说废话
      （个别服务商不支持该参数，会自动去掉后重试一次）
    - 失败按指数退避重试；全部失败返回 None，由上层降级
    """
    provider = provider_of()
    if not provider:
        return None

    url = provider["base_url"] + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if provider["api_key"]:
        headers["Authorization"] = "Bearer " + provider["api_key"]

    timeout = timeout or provider["timeout"]
    if retries is None:
        retries = provider["max_retries"]
    payload = {
        "model": provider["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_err = None
    for attempt in range(retries + 1):
        body = json.dumps(payload).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices") or []
                if not choices:
                    last_err = "接口返回中没有 choices 字段"
                    break
                return choices[0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            last_err = "HTTP %s %s" % (e.code, detail)
            # 400 常常是 response_format 不被支持 → 去掉再试一次
            if e.code == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                continue
            # 401/403 是 Key 问题，重试无意义
            if e.code in (401, 403):
                break
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:200])

        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))       # 指数退避

    if last_err:
        try:
            from config import DATA_DIR
            import os
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "llm-error.log"), "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), last_err))
        except Exception:
            pass
    return None


def test_connection():
    """
    连通性测试（设置页「测试连接」按钮）。返回 (ok, message)。
    message 会直接显示给用户，所以要写清原因和下一步怎么做。
    """
    provider = provider_of()
    if not provider:
        return False, ("未完成配置：请先选择服务商、填入 API Key（本地 Ollama 可留空）"
                       "并确认已启用。")
    t0 = time.time()
    result = llm_chat(
        [{"role": "user", "content": "只回复两个字：正常"}],
        max_tokens=20, temperature=0, json_mode=False, retries=0, timeout=30)
    cost = time.time() - t0
    if result is None:
        return False, ("连接失败：%s 未能返回结果。请检查 API Key 是否正确、"
                       "Base URL 是否为 %s、以及本机网络能否访问该地址。"
                       % (provider["model"], provider["base_url"]))
    return True, "连接成功（%.1f 秒）：模型 %s 返回「%s」" % (
        cost, provider["model"], str(result).strip()[:20])


# ---------------------------------------------------------------------------
# JSON 解析容错：模型经常把 JSON 包在 ```json 里，或前后带解释文字
# ---------------------------------------------------------------------------
def parse_json_loose(text):
    """尽力从模型输出里抠出 JSON 对象/数组，失败返回 None"""
    if not text:
        return None
    s = text.strip()

    # 去掉 markdown 代码块围栏
    if s.startswith("```"):
        parts = s.split("```")
        if len(parts) >= 2:
            s = parts[1]
            if s.lstrip().lower().startswith("json"):
                s = s.lstrip()[4:]
        s = s.strip()

    try:
        return json.loads(s)
    except Exception:
        pass

    # 退一步：截取第一个 { 或 [ 到最后一个 } 或 ]
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return None


def _split_chunks(items, chunk_chars=CHUNK_CHARS):
    """
    把 [(sec, text), ...] 按字符数切成若干块。
    返回 [{"items": [(sec,text)...], "start": 秒, "end": 秒, "text": "..."}, ...]
    """
    chunks = []
    cur_items, cur_len = [], 0

    def flush():
        if not cur_items:
            return
        chunks.append({
            "items": list(cur_items),
            "start": cur_items[0][0],
            "end": cur_items[-1][0],
            "text": " ".join(t for _, t in cur_items),
        })

    for sec, text in items:
        # 单条就超长时硬切，避免一块塞爆上下文
        if len(text) > chunk_chars:
            flush()
            cur_items, cur_len = [], 0
            for k in range(0, len(text), chunk_chars):
                piece = text[k:k + chunk_chars]
                chunks.append({"items": [(sec, piece)], "start": sec,
                               "end": sec, "text": piece})
            continue
        if cur_len + len(text) > chunk_chars:
            flush()
            cur_items, cur_len = [], 0
        cur_items.append((sec, text))
        cur_len += len(text)
    flush()
    return chunks


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
SYS_NOTES = ("你是一位资深的学习笔记整理专家，擅长从课堂讲解、视频字幕和文章里"
             "提炼真正有学习价值的知识点。你输出的标题是概念名而不是句子片段，"
             "你从不编造原文没有的内容。你的输出必须是严格的 JSON。")

SYS_PLAN = ("你是课程结构分析师。你会根据文稿内容判断这门课讲了哪几个主题，"
            "并按主题把文稿切成连续的段落。你只输出 JSON。")


def _points_prompt(chunk_text, index, total, max_points, quality):
    extra = ""
    if quality in ("standard", "deep"):
        extra = ('\n   - "plain_explain": 用生活化类比或大白话讲清这个概念，'
                 '面向零基础，80字以内。不要复述原句。')
    if quality == "deep":
        extra += '\n   - "pitfall": 这个知识点最容易错/最容易混淆的地方，40字以内。'

    return """请从下面这段课程文稿（第 %d/%d 段）中提炼知识点。

要求：
1. 提炼 1~%d 个真正有学习价值的知识点；如果这段只是寒暄、过渡、广告、重复，可以少提甚至返回空数组。
2. 每个知识点的字段：
   - "title": 概念名称，8~20字。必须是名词性短语（如「WBI 签名的生成流程」），严禁把一句话截断当标题。
   - "gist": 这个知识点讲的是什么，一句话，40字以内。
   - "detail": 展开说明，2~3 句，说明原理、步骤或结论，150字以内。只允许使用文稿中出现的信息。
   - "keywords": 2~4 个关键词的数组。%s
3. 输出 JSON 对象：{"points": [ ... ]}
4. 不要输出 JSON 以外的任何文字。

文稿：
%s""" % (index, total, max_points, extra, chunk_text)


def _plan_prompt(sample_text, max_chapters):
    return """下面是一门课程的文稿片段。请判断它大致讲了哪几个主题，并给出章节目录。

要求：
1. 切出 2~%d 个章节，按课程推进顺序排列。
2. 每章给出：
   - "title": 章节标题，10~22字，要具体（如「WBI 签名与字幕接口调用」），不要写「第一部分」这种空标题。
   - "hint": 用 20 字以内说明这章覆盖的内容，帮我判断后续文稿属于哪一章。
3. 输出 JSON 对象：{"chapters": [{"title": "...", "hint": "..."}, ...]}
4. 只输出 JSON。

文稿片段：
%s""" % (max_chapters, sample_text)


def _summary_prompt(text):
    return """基于下面的课程材料，生成课程概述与必背考点。

要求：
1. "overview": 2~3 句话，说明这门课讲了什么、学完能做什么。要具体，不要「本课程内容充实」这类空话。
2. "exam_points": 5~8 条必背考点，每条一句话，必须是可考查的具体结论（公式、步骤、定义、易错点），不要泛泛而谈。
3. 输出 JSON 对象：{"overview": "...", "exam_points": ["...", ...]}
4. 只输出 JSON。

材料：
%s""" % text


def _enhance_prompt(chapter_title, points):
    brief = [{"title": p.get("title", ""), "gist": p.get("gist", "")} for p in points]
    return """下面是「%s」这一章的知识点。请为每个知识点补充通俗解释。

要求：
1. 每个知识点输出：
   - "one_liner": 一句话掌握，用大白话概括，30字以内。
   - "plain_explain": 通俗解释，用生活化类比讲明白，面向零基础，100字以内。
2. 输出 JSON 对象：{"items": [{"title": "知识点标题（必须与输入完全一致）", "one_liner": "...", "plain_explain": "..."}]}
3. 只输出 JSON。

知识点：
%s""" % (chapter_title, json.dumps(brief, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# 各级提炼能力
# ---------------------------------------------------------------------------
def llm_extract_points(chunk_text, index=1, total=1, max_points=5, quality="standard"):
    """从一段文稿提炼知识点，返回 list（失败返回 []）"""
    if not chunk_text.strip():
        return []
    raw = llm_chat(
        [{"role": "system", "content": SYS_NOTES},
         {"role": "user", "content": _points_prompt(chunk_text, index, total,
                                                    max_points, quality)}],
        max_tokens=3000, temperature=0.2)
    data = parse_json_loose(raw)
    if data is None:
        return []
    if isinstance(data, dict):
        points = data.get("points") or data.get("items") or []
    elif isinstance(data, list):
        points = data
    else:
        points = []
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()
        if not title:
            continue
        kws = p.get("keywords")
        if not isinstance(kws, list):
            kws = []
        out.append({
            "title": title[:60],
            "gist": str(p.get("gist") or "").strip()[:200],
            "detail": str(p.get("detail") or "").strip()[:600],
            "plain_explain": str(p.get("plain_explain") or "").strip()[:500],
            "pitfall": str(p.get("pitfall") or "").strip()[:200],
            "keywords": [str(k)[:20] for k in kws[:6]],
        })
    return out


def llm_plan_chapters(chunks, max_chapters=12):
    """
    用文稿开头部分规划章节结构，返回 [{"title","hint"}, ...]（失败返回 []）
    只看开头 3000 字：足够判断课程主题，又省 token。
    """
    if not chunks:
        return []
    sample = " ".join(c["text"] for c in chunks[:2])[:3000]
    raw = llm_chat(
        [{"role": "system", "content": SYS_PLAN},
         {"role": "user", "content": _plan_prompt(sample, max_chapters)}],
        max_tokens=1200, temperature=0.3)
    data = parse_json_loose(raw)
    if not isinstance(data, dict):
        return []
    chapters = data.get("chapters") or []
    out = []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        title = str(ch.get("title") or "").strip()
        if title:
            out.append({"title": title[:40],
                        "hint": str(ch.get("hint") or "").strip()[:40]})
    return out[:max_chapters]


def llm_enhance_note(kmap, transcript_excerpt="", source_url=""):
    """
    为知识图谱补「一句话掌握 + 通俗解释」。
    按章节分组并发调用，避免一次塞几百个知识点把上下文挤爆。
    返回增强后的 kmap，或 None（不可用/全部失败）。
    """
    if not llm_available():
        return None
    chapters = kmap.get("chapters") or []
    if not chapters:
        return None

    tasks = []
    for ch in chapters:
        pts = ch.get("points") or []
        if pts:
            tasks.append((ch, pts))

    def work(task):
        ch, pts = task
        raw = llm_chat(
            [{"role": "system", "content": SYS_NOTES},
             {"role": "user", "content": _enhance_prompt(ch.get("title", ""), pts)}],
            max_tokens=2500, temperature=0.4)
        return ch, parse_json_loose(raw)

    results = _map_maybe_parallel(work, tasks, max_workers=4)

    touched = 0
    for ch, data in results:
        if not isinstance(data, dict):
            continue
        items = data.get("items") or data.get("points") or []
        if not isinstance(items, list):
            continue
        by_title = {}
        for it in items:
            if isinstance(it, dict) and it.get("title"):
                by_title[str(it["title"]).strip()] = it
        for p in ch.get("points") or []:
            it = by_title.get(p.get("title", ""))
            if not it:
                continue
            if it.get("one_liner"):
                p["one_liner"] = str(it["one_liner"]).strip()[:120]
                touched += 1
            if it.get("plain_explain"):
                p["plain_explain"] = str(it["plain_explain"]).strip()[:500]

    if touched == 0:
        return None
    kmap["llm_enhanced"] = True
    return kmap


def llm_generate_summary(transcript_excerpt, source_url=""):
    """生成概述 + 必背考点，返回 dict 或 None"""
    if not llm_available() or not transcript_excerpt.strip():
        return None
    raw = llm_chat(
        [{"role": "system", "content": SYS_NOTES},
         {"role": "user", "content": _summary_prompt(transcript_excerpt[:SUMMARY_CHARS])}],
        max_tokens=2000, temperature=0.3)
    data = parse_json_loose(raw)
    if not isinstance(data, dict):
        return None
    overview = str(data.get("overview") or "").strip()
    eps = data.get("exam_points") or []
    if not overview and not eps:
        return None
    if not isinstance(eps, list):
        eps = []
    return {
        "overview": overview[:800],
        "exam_points": [str(e).strip()[:200] for e in eps if str(e).strip()][:10],
    }


def _map_maybe_parallel(fn, items, max_workers=4):
    """
    并发执行；任意一个抛异常都当作该项失败（返回 (item, None)），不影响其他项。
    并发是为了长视频（几十块）时把等待时间压下来。
    """
    if not items:
        return []
    if ThreadPoolExecutor is None or len(items) == 1 or max_workers <= 1:
        out = []
        for it in items:
            try:
                out.append(fn(it))
            except Exception:
                out.append((it, None) if isinstance(it, tuple) and len(it) == 2 else None)
        return out

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as ex:
        futures = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for fut, i in futures.items():
            try:
                results[i] = fut.result()
            except Exception:
                it = items[i]
                results[i] = (it, None) if isinstance(it, tuple) and len(it) == 2 else None
    return results


# ---------------------------------------------------------------------------
# 一站式：文稿 -> 知识图谱（这是「提高准确率」的主入口）
# ---------------------------------------------------------------------------
def llm_build_knowledge_map(items, title="学习资源", max_chapters=12,
                            points_per_chapter=5, quality="standard",
                            progress=None):
    """
    用大模型从文稿构建知识图谱。

    items: [(秒, 文本), ...]，秒只在视频场景有意义（网页/文本传 0）
    返回 kmap dict；失败或不可用时返回 None，上层回落到启发式提炼。
    """
    if not llm_available() or not items:
        return None

    def say(step, msg):
        if progress:
            try:
                progress(step, msg)
            except Exception:
                pass

    chunks = _split_chunks(items)
    if not chunks:
        return None
    say(4, "大模型正在规划章节结构…")

    # 1) 规划章节（失败就退化成「一块一章」）
    plan = llm_plan_chapters(chunks, max_chapters=max_chapters)

    # 2) 逐块提炼知识点（并发）
    say(4, "大模型正在提炼 %d 段内容的知识点…" % len(chunks))

    def work(idx_chunk):
        idx, chunk = idx_chunk
        return idx, llm_extract_points(chunk["text"], idx + 1, len(chunks),
                                       points_per_chapter, quality)

    pairs = _map_maybe_parallel(work, list(enumerate(chunks)), max_workers=4)

    chunk_points = {}
    for pair in pairs:
        if isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], int):
            chunk_points[pair[0]] = pair[1] or []

    total_points = sum(len(v) for v in chunk_points.values())
    if total_points == 0:
        return None       # 全失败，让上层降级

    # 时间轴判定：不能写 any(s for s, _ in items)，因为第一条字幕常常是 0 秒，
    # 布尔判断会把 0 当假，导致视频笔记莫名其妙丢掉时间标签。
    # 用「跨度足够大」判断，与 server.py 的启发式提炼保持一致。
    secs = [s for s, _ in items]
    has_timeline = bool(secs) and (max(secs) - min(secs)) >= 120

    # 3) 把块归入章节
    if plan:
        # 均匀分配：把块按顺序切给各章（LLM 已按顺序给主题）
        n_ch = len(plan)
        per = max(1, (len(chunks) + n_ch - 1) // n_ch)
        groups = []
        for ci in range(n_ch):
            chunk_ids = list(range(ci * per, min((ci + 1) * per, len(chunks))))
            if chunk_ids:
                groups.append((plan[ci]["title"], chunk_ids))
        # 有块没被分到（块数少于章数时的尾部），并入最后一章
        assigned = set()
        for _, ids in groups:
            assigned.update(ids)
        leftover = [i for i in range(len(chunks)) if i not in assigned]
        if leftover and groups:
            groups[-1][1].extend(leftover)
    else:
        group_size = max(1, (len(chunks) + max_chapters - 1) // max_chapters)
        groups = []
        for i in range(0, len(chunks), group_size):
            ids = list(range(i, min(i + group_size, len(chunks))))
            groups.append(("第%d部分" % (len(groups) + 1), ids))

    # 4) 组装 chapters（带时间轴）
    chapters = []
    for gi, (chap_title, chunk_ids) in enumerate(groups):
        pts = []
        seen_titles = set()
        start_sec = None
        for ci in chunk_ids:
            if ci >= len(chunks):
                continue
            ch = chunks[ci]
            if start_sec is None:
                start_sec = ch["start"]
            for p in chunk_points.get(ci, []):
                key = re.sub(r"\s+", "", p["title"])[:12]
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                pts.append(p)
        if not pts:
            continue

        chap = {
            "title": ("第%d节 [%s] %s" % (gi + 1, _tcs(start_sec or 0), chap_title)
                      if has_timeline else chap_title),
            "points": [],
        }
        for p in pts[:max(points_per_chapter, 1) * 2]:
            point = {
                "title": p["title"],
                "start_sec": int(start_sec) if (has_timeline and start_sec is not None) else None,
                "start_label": _tcs(start_sec) if (has_timeline and start_sec is not None) else None,
                "summary": (p.get("detail") or p.get("gist") or "")[:400],
                "gist": p.get("gist", ""),
                "keywords": p.get("keywords", []),
                "excerpt": _excerpt_for(p, chunks[chunk_ids[0]]["text"] if chunk_ids else ""),
            }
            if p.get("plain_explain"):
                point["plain_explain"] = p["plain_explain"]
            if p.get("pitfall"):
                point["pitfall"] = p["pitfall"]
            chap["points"].append(point)
        if chap["points"]:
            chapters.append(chap)

    if not chapters:
        return None

    kmap = {
        "course_title": title,
        "chapters": chapters,
        "has_timeline": any(s for s, _ in items),
        "duration_sec": int(max(s for s, _ in items)) if items else 0,
        "llm_generated": True,
    }
    return kmap


def _excerpt_for(point, source_text):
    """
    从原文里找与知识点最相关的一句作为「原文摘录」。
    没找到就返回空，不硬凑（旧版会硬塞前两句，造成摘录与知识点无关）。
    """
    if not source_text:
        return []
    kws = point.get("keywords") or []
    sentences = re.split(r"(?<=[。！？；!?])\s*", source_text)
    if kws:
        for s in sentences:
            s = s.strip()
            if len(s) >= 8 and any(k and k in s for k in kws):
                return [s[:200]]
    return []


def _tcs(seconds):
    seconds = int(seconds or 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return "%02d:%02d:%02d" % (h, m, s)


if __name__ == "__main__":
    # 自测：打印配置状态 + 连通性
    info = provider_info()
    print("LLM 状态:", json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("active"):
        ok, msg = test_connection()
        print("连通测试:", ok, msg)
