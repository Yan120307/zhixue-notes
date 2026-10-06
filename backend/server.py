#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记本地后端服务 — 接入正式整理能力

提供 REST API 供前端平台调用，复用 video-note-master 技能脚本：
  - 字幕抓取（fetch_subtitles.py）
  - 基础提炼（分章节 + 关键句提取，生成 knowledge_map.json）
  - PDF 导出（export_pdf.py）
  - 思维导图生成（gen_mindmap.py）

用法：
    python server.py [--port 8766] [--host 127.0.0.1]

依赖：与技能脚本相同（requests / reportlab / matplotlib）
"""
import argparse
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm import llm_available, llm_enhance_note, llm_generate_summary

# 脚本目录：仓库自带 scripts/（GitHub 克隆 / Docker 部署开箱即用，零外部依赖）
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_DIR, "scripts")

# 数据目录：用户主目录下（跨平台，不污染仓库）
DATA_DIR = os.path.join(os.path.expanduser("~"), ".zhixue-notes")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# 任务状态
_tasks = {}
_lock = threading.Lock()

LOG_FILE = os.path.join(DATA_DIR, "zhixue-backend.log")

def _log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


def detect_type(text):
    text = (text or "").strip()
    if not text:
        return None
    if re.search(r"bilibili\.com|b23\.tv", text, re.I):
        return {"key": "bilibili", "name": "B站视频"}
    if re.search(r"youtube\.com|youtu\.be", text, re.I):
        return {"key": "youtube", "name": "YouTube 视频"}
    if re.search(r"pan\.baidu\.com|aliyundrive|alipan|quark|123pan|lanzou", text, re.I):
        return {"key": "pan", "name": "网盘分享文件夹"}
    if re.search(r"\.pdf(\?|$)", text, re.I):
        return {"key": "pdf", "name": "PDF 文档"}
    if re.search(r"\.(docx?|pptx?|md|txt)(\?|$)", text, re.I):
        return {"key": "doc", "name": "文档资料"}
    if re.search(r"^https?://", text, re.I):
        return {"key": "web", "name": "网页文章"}
    return {"key": "text", "name": "文本内容"}


def fetch_subtitles(url, out_dir, type_key):
    if type_key not in ("bilibili", "youtube"):
        return False, None, "非视频资源，跳过字幕抓取"
    script = os.path.join(SCRIPTS, "fetch_subtitles.py")
    if not os.path.exists(script):
        return False, None, "字幕抓取脚本不存在"
    meta_path = os.path.join(out_dir, "video_meta.json")
    try:
        proc = subprocess.Popen(
            [sys.executable, script, url, "--out-dir", out_dir, "--fmt", "srt",
             "--meta-out", meta_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if os.name == "nt" else 0)
        start = time.time()
        while time.time() - start < 120:
            rc = proc.poll()
            if rc is not None:
                if rc == 0:
                    for f in os.listdir(out_dir):
                        if f.endswith(".srt"):
                            return True, os.path.join(out_dir, f), "ok"
                    return False, None, "字幕抓取成功但未找到文件"
                return False, None, "returncode=%s" % rc
            time.sleep(0.5)
        proc.kill()
        return False, None, "字幕抓取超时（120秒）"
    except Exception as e:
        return False, None, str(e)


def _read_meta(task_dir):
    """读取 fetch_subtitles 落盘的视频元信息（真实标题等）"""
    try:
        p = os.path.join(task_dir, "video_meta.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def fetch_webpage(url, task_dir):
    """抓取网页正文，返回 (ok, title, paragraphs, error)"""
    script = os.path.join(SCRIPTS, "fetch_webpage.py")
    if not os.path.exists(script):
        return False, None, [], "网页抓取脚本不存在"
    out_path = os.path.join(task_dir, "web_content.json")
    ok = _run_script([sys.executable, script, url, "--out", out_path], timeout=90)
    if not os.path.exists(out_path):
        return False, None, [], "网页抓取失败（脚本未能运行）"
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None, [], "网页抓取结果解析失败"
    if data.get("error"):
        return False, data.get("title"), [], data["error"]
    return True, data.get("title"), data.get("paragraphs") or [], None


def parse_srt(srt_path):
    items = []
    if not srt_path or not os.path.exists(srt_path):
        return items
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\s*\n", content.strip())
    for blk in blocks:
        lines = blk.strip().split("\n")
        if len(lines) < 3:
            continue
        m = re.match(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})", lines[1])
        if not m:
            continue
        start = m.group(1).replace(",", ".")
        h, mi, s = start.split(":")
        sec = int(h) * 3600 + int(mi) * 60 + float(s)
        text = " ".join(lines[2:]).strip()
        if text:
            items.append((sec, text))
    return items


def tcs(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return "%02d:%02d:%02d" % (h, m, s)


def split_sentences(full_text):
    """按句末标点分句，返回有效句列表（小数点后跟数字时不切分，如 3.0）"""
    parts = re.split(r"(?<=[。！？；!?])\s*|(?<=[.])(?!\d)\s*", full_text)
    return [s.strip() for s in parts if len(s.strip()) >= 6]


def info_score(s):
    """信息量评分：数值/英文术语/长度加权"""
    return len(re.findall(r"\d|[A-Z]{2,}", s)) + min(len(s), 40) / 40.0


def _find_excerpt(sent, texts):
    """在原始条目里找包含要点句的原文，返回该条目及相邻上下文"""
    key = sent[:10]
    for i, t in enumerate(texts):
        if key and key in t:
            out = [texts[max(0, i - 1)], t]
            if i + 1 < len(texts):
                out.append(texts[i + 1])
            return out
    return texts[:2]


def basic_extract(items, title="学习资源", max_chapters=10):
    """
    从内容条目中提炼知识图谱。
    items: [(sec, text), ...] —— 有真实时间轴（视频字幕）时按 300 秒分章；
    无时间轴（网页段落/纯文本）时按条目数分章。
    """
    if not items:
        return {"course_title": title, "chapters": [], "has_timeline": False}

    secs = [s for s, _ in items]
    has_timeline = (max(secs) - min(secs)) >= 120
    kmap = {"course_title": title, "chapters": [], "has_timeline": has_timeline}

    if has_timeline:
        kmap["duration_sec"] = int(max(secs))
        seg_len = 300
        groups = []
        cur = []
        cur_start = items[0][0]
        for sec, text in items:
            if sec - cur_start > seg_len and cur:
                groups.append((cur_start, cur))
                cur = []
                cur_start = sec
            cur.append((sec, text))
        if cur:
            groups.append((cur_start, cur))
    else:
        n = len(items)
        group_size = max(1, (n + max_chapters - 1) // max_chapters)
        groups = [(0, items[i:i + group_size]) for i in range(0, n, group_size)]
        kmap["duration_sec"] = 0

    chapters = []
    for gi, (start, group) in enumerate(groups):
        texts = [t for _, t in group]
        full = " ".join(texts)
        sentences = split_sentences(full)
        if not sentences:
            # 没有可分句子时，用整段做单要点
            sentences = [full[:60]] if full.strip() else []
        ranked = sorted(sentences, key=info_score, reverse=True)

        # 章节名 = 该章信息量最高句前 18 字
        chap_name = ranked[0][:18].strip() if ranked else "内容 %d" % (gi + 1)
        if has_timeline:
            chap_title = "第%d节 [%s] %s" % (gi + 1, tcs(start), chap_name)
        else:
            chap_title = "第%d节 %s" % (gi + 1, chap_name)

        n_points = max(1, min(3, len(sentences) // 4 + 1))
        points = []
        used = set()
        for sent in ranked[:n_points * 2]:
            if len(points) >= n_points:
                break
            if sent[:20] in used:
                continue
            used.add(sent[:20])
            # 摘要 = 要点句 + 章内另外 2 句补充
            summary_parts = [sent]
            for cand in sentences:
                if len(summary_parts) >= 3:
                    break
                if cand[:20] not in used and cand != sent:
                    summary_parts.append(cand)
                    used.add(cand[:20])
            points.append({
                "title": sent[:36],
                "start_sec": int(start) if has_timeline else None,
                "start_label": tcs(start) if has_timeline else None,
                "summary": " ".join(summary_parts)[:220],
                "excerpt": _find_excerpt(sent, texts),
            })
        chapters.append({"title": chap_title, "points": points})

    kmap["chapters"] = chapters[:max_chapters]
    return kmap


def build_notes_md(kmap, srt_items, source_url, fmts):
    title = kmap.get("course_title", "学习笔记")
    lines = ["# %s — 重点笔记" % title, ""]
    lines.append("> **来源**: %s" % source_url)
    lines.append("> **时长**: %s | **生成日期**: %s" % (
        tcs(kmap.get("duration_sec", 0)) if kmap.get("duration_sec") else "—",
        time.strftime("%Y-%m-%d %H:%M")))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 概述")
    lines.append("")
    if kmap.get("llm_summary") and kmap["llm_summary"].get("overview"):
        lines.append(kmap["llm_summary"]["overview"])
    else:
        chapters = kmap.get("chapters", [])
        # 概述取每章首要点标题（比拼接句子更结构化）
        pts = []
        for ch in chapters:
            ch_points = ch.get("points", [])
            if ch_points:
                pts.append(ch_points[0]["title"])
        pts = pts[:8]
        if pts:
            lines.append("本资源共 %d 章，核心要点包括：%s。" % (
                len(chapters), "；".join(pts)))
        else:
            lines.append("本笔记由智学笔记平台自动整理。")
    lines.append("")
    lines.append("### 核心知识图谱")
    lines.append("")
    for ch in kmap.get("chapters", []):
        lines.append("- %s" % ch["title"])
        for p in ch.get("points", []):
            lines.append("  - %s" % p["title"])
    lines.append("")
    lines.append("---")
    lines.append("")
    for ch in kmap.get("chapters", []):
        lines.append("## %s" % ch["title"])
        lines.append("")
        for p in ch.get("points", []):
            lines.append("### %s" % p["title"])
            lines.append("")
            if p.get("start_label"):
                lines.append("> 视频时间：%s" % p["start_label"])
                lines.append("")
            lines.append("**核心要点**：%s" % p.get("summary", ""))
            lines.append("")
            if p.get("one_liner"):
                lines.append("**一句话掌握**：%s" % p["one_liner"])
                lines.append("")
            if p.get("excerpt"):
                lines.append("**原文摘录**：")
                for t in p["excerpt"][:3]:
                    lines.append("> %s" % str(t)[:200])
                lines.append("")
            if p.get("plain_explain"):
                lines.append("**通俗解释**：")
                lines.append("")
                lines.append(p["plain_explain"])
                lines.append("")
            else:
                lines.append("**通俗解释**：（配置 DASHSCOPE_API_KEY 环境变量可自动生成 AI 通俗解释）")
                lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("## 重点考点清单")
    lines.append("")
    if kmap.get("llm_summary") and kmap["llm_summary"].get("exam_points"):
        lines.append("### 必背考点（AI 生成）")
        for ep in kmap["llm_summary"]["exam_points"]:
            lines.append("- [ ] %s" % ep)
        lines.append("")
    lines.append("### 核心概念")
    for ch in kmap.get("chapters", []):
        for p in ch.get("points", []):
            lines.append("- [ ] %s" % p["title"])
    lines.append("")
    lines.append("> 本笔记由智学笔记平台自动生成。配置 DASHSCOPE_API_KEY 或 OPENAI_API_KEY 环境变量可启用 AI 通俗解释与考点提炼。")
    lines.append("> AI生成")
    return "\n".join(lines)


def _run_script(cmd, timeout=90):
    """安全运行脚本：Popen + 轮询，永不阻塞主流程"""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if os.name == "nt" else 0)  # DETACHED_PROCESS
        start = time.time()
        while time.time() - start < timeout:
            rc = proc.poll()
            if rc is not None:
                return rc == 0
            time.sleep(0.5)
        proc.kill()
        return False
    except Exception:
        return False


_playwright_cache = {}


def playwright_available():
    """检测 playwright 是否可用（缓存结果）"""
    if "ok" not in _playwright_cache:
        try:
            import importlib.util
            _playwright_cache["ok"] = importlib.util.find_spec("playwright") is not None
        except Exception:
            _playwright_cache["ok"] = False
    return _playwright_cache["ok"]


_fw_cache = {}


def faster_whisper_available():
    """检测 faster-whisper 是否可用（缓存结果）"""
    if "ok" not in _fw_cache:
        try:
            import importlib.util
            _fw_cache["ok"] = importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            _fw_cache["ok"] = False
    return _fw_cache["ok"]


def transcribe_video(url, task_dir, update):
    """
    无字幕视频：下载音频流 + faster-whisper 本地转写。
    长任务：Popen 转写脚本，轮询进度文件实时回报。返回 (ok, srt_path, error)
    """
    script = os.path.join(SCRIPTS, "transcribe_audio.py")
    if not os.path.exists(script):
        return False, None, "转写脚本不存在"
    prog_path = os.path.join(task_dir, "transcode_progress.json")
    try:
        proc = subprocess.Popen(
            [sys.executable, script, url, "--task-dir", task_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if os.name == "nt" else 0)
    except Exception as e:
        return False, None, str(e)[:150]

    last_msg = ""
    rc = None
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        try:
            if os.path.exists(prog_path):
                with open(prog_path, "r", encoding="utf-8") as f:
                    pr = json.load(f)
                pct = min(100.0, float(pr.get("percent") or 0))
                stage = pr.get("stage")
                msg = pr.get("message") or ""
                if msg != last_msg:
                    last_msg = msg
                    if stage == "download":
                        update(2, msg, progress=10 + pct * 0.15)
                    elif stage == "transcribe":
                        update(3, msg, progress=25 + pct * 0.20)
        except Exception:
            pass
        time.sleep(2)

    srt = os.path.join(task_dir, "transcript.srt")
    if rc == 0 and os.path.exists(srt):
        return True, srt, None
    err = "音频转写进程异常退出"
    try:
        if os.path.exists(prog_path):
            with open(prog_path, "r", encoding="utf-8") as f:
                pr = json.load(f)
            if pr.get("message"):
                err = str(pr["message"])[:150]
    except Exception:
        pass
    return False, None, err


_fw_cache = {}


def faster_whisper_available():
    """检测 faster-whisper 是否可用（缓存结果）"""
    if "ok" not in _fw_cache:
        try:
            import importlib.util
            _fw_cache["ok"] = importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            _fw_cache["ok"] = False
    return _fw_cache["ok"]


def transcribe_video(url, task_dir, update):
    """
    无字幕视频：下载音频流 + faster-whisper 本地转写。
    长任务：Popen 转写脚本，轮询进度文件实时回报。返回 (ok, srt_path, error)
    """
    script = os.path.join(SCRIPTS, "transcribe_audio.py")
    if not os.path.exists(script):
        return False, None, "转写脚本不存在"
    prog_path = os.path.join(task_dir, "transcode_progress.json")
    try:
        proc = subprocess.Popen(
            [sys.executable, script, url, "--task-dir", task_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if os.name == "nt" else 0)
    except Exception as e:
        return False, None, str(e)[:150]

    last_msg = ""
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        try:
            if os.path.exists(prog_path):
                with open(prog_path, "r", encoding="utf-8") as f:
                    pr = json.load(f)
                pct = min(100.0, float(pr.get("percent") or 0))
                stage = pr.get("stage")
                msg = pr.get("message") or ""
                if msg != last_msg:
                    last_msg = msg
                    if stage == "download":
                        update(2, msg, progress=10 + pct * 0.15)
                    elif stage == "transcribe":
                        update(3, msg, progress=25 + pct * 0.20)
                    elif stage == "done":
                        pass
        except Exception:
            pass
        time.sleep(2)

    srt = os.path.join(task_dir, "transcript.srt")
    if rc == 0 and os.path.exists(srt):
        return True, srt, None
    err = "音频转写进程异常退出"
    try:
        if os.path.exists(prog_path):
            with open(prog_path, "r", encoding="utf-8") as f:
                pr = json.load(f)
            if pr.get("message"):
                err = str(pr["message"])[:150]
    except Exception:
        pass
    return False, None, err


def export_anki(kmap_path, out_path):
    """knowledge_map → Anki 卡片 TSV"""
    script = os.path.join(SCRIPTS, "export_anki.py")
    return _run_script([sys.executable, script, kmap_path, "-o", out_path], timeout=30)


def export_obsidian(task_dir, vault, folder):
    """把任务目录笔记写入 Obsidian vault"""
    script = os.path.join(SCRIPTS, "export_obsidian.py")
    ok = _run_script([sys.executable, script, task_dir, "--vault", vault, "--folder", folder], timeout=30)
    return ok


def export_pdf(md_path, out_pdf, img_base=None):
    script = os.path.join(SCRIPTS, "export_pdf.py")
    cmd = [sys.executable, script, md_path, "-o", out_pdf]
    if img_base:
        cmd += ["--img-base", img_base]
    return _run_script(cmd, timeout=60), ""


def gen_mindmap(kmap_path, out_png):
    script = os.path.join(SCRIPTS, "gen_mindmap.py")
    return _run_script([sys.executable, script, kmap_path, "-o", out_png], timeout=60), ""


def process_task(task_id, url, fmts, type_info):
    task_dir = os.path.join(TASKS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    def update(step, log, done=False, result=None, error=None, progress=None):
        d = {"step": step, "log": log, "done": done,
             "result": result, "error": error, "ts": time.time()}
        if progress is not None:
            d["progress"] = progress
        with _lock:
            _tasks[task_id].update(d)

    title = None
    fail = None
    srt_items = []
    video_duration = 0

    update(0, "正在识别资源类型…", progress=3)
    update(1, "已识别：%s" % type_info["name"], progress=8)

    type_key = type_info["key"]

    if type_key in ("bilibili", "youtube"):
        update(2, "正在抓取字幕…", progress=10)
        ok, srt_path, msg = fetch_subtitles(url, task_dir, type_key)
        meta = _read_meta(task_dir) or {}
        title = meta.get("title")
        video_duration = meta.get("duration") or 0
        if ok:
            srt_items = parse_srt(srt_path)
            update(3, "字幕抓取成功，共 %d 条，正在提炼重点…" % len(srt_items), progress=45)
            if len(srt_items) < 8:
                fail = "该视频字幕内容过少（%d 条），无法整理出有效笔记" % len(srt_items)
        else:
            if type_key == "bilibili" and faster_whisper_available():
                # 无 CC 字幕 → 自动本地音频转写（faster-whisper）
                update(2, "《%s》没有 CC 字幕，自动转入本地音频转写（下载音频中，请耐心等待）…" % str(title or "该视频")[:40], progress=10)
                ok2, srt_path2, terr = transcribe_video(url, task_dir, update)
                if ok2:
                    srt_items = parse_srt(srt_path2)
                    update(3, "音频转写完成，共 %d 条，正在提炼重点…" % len(srt_items), progress=45)
                    if len(srt_items) < 8:
                        fail = "转写内容过少（%d 条），可能该视频无语音内容" % len(srt_items)
                else:
                    fail = "《%s》无 CC 字幕且本地转写失败：%s。可复制视频文稿粘贴到输入框" % (
                        str(title or "该视频")[:30], terr[:80])
            else:
                if type_key == "bilibili":
                    reason = ("该视频没有 CC 字幕（B站字幕需 UP 主上传），且本机未安装 faster-whisper。"
                              "安装后可自动转写音频：pip install faster-whisper；"
                              "或复制视频文稿粘贴到输入框")
                else:
                    reason = ("YouTube 字幕抓取失败（%s）。请确认已安装 yt-dlp，"
                              "或复制文稿粘贴到输入框" % msg[:60])
                if title:
                    reason = "《%s》%s" % (str(title)[:40], reason)
                fail = reason
    elif type_key == "web":
        update(2, "正在抓取网页正文…", progress=12)
        ok, wtitle, paras, werr = fetch_webpage(url, task_dir)
        if wtitle:
            title = wtitle
        if ok and len(paras) >= 3:
            srt_items = [(0, p) for p in paras]
            update(3, "正文抓取成功，共 %d 段，正在提炼重点…" % len(paras), progress=45)
        else:
            fail = werr or "网页正文抓取失败，请直接复制网页文字粘贴到输入框"
    elif type_key == "text":
        sents = split_sentences(url)
        sents = [s for s in sents if len(s.strip()) >= 6]
        if len(sents) < 3:
            fail = "输入内容太短（有效句不足 3 句），请粘贴更完整的学习内容"
        else:
            srt_items = [(0, s) for s in sents]
            title = re.sub(r"\s+", " ", url.strip())[:24]
            update(3, "已接收 %d 句内容，正在提炼重点…" % len(sents), progress=45)
    elif type_key == "pan":
        fail = ("网盘分享链接暂不支持自动整理。"
                "请打开分享链接复制资料文字，或直接粘贴视频链接到输入框")
    else:  # pdf / doc
        fail = "PDF / Word 文档暂不支持自动抓取，请复制文档文字后粘贴到输入框"

    if fail:
        update(3, "整理失败：%s" % fail[:150], done=True, error=fail)
        return

    kmap = basic_extract(srt_items, title=(title or "%s整理笔记" % type_info["name"]))
    if type_key in ("bilibili", "youtube") and video_duration:
        kmap["duration_sec"] = int(video_duration)

    # LLM 增强（配置了 API Key 时自动启用）
    llm_on = llm_available()
    if llm_on and srt_items:
        update(4, "大模型增强中：通俗解释 + 考点提炼…", progress=55)
        excerpt = " ".join(t for _, t in srt_items)[:4000]
        enhanced = llm_enhance_note(kmap, transcript_excerpt=excerpt, source_url=url)
        if enhanced:
            kmap = enhanced
        summary = llm_generate_summary(excerpt, url)
        if summary:
            kmap["llm_summary"] = summary
    else:
        update(4, "未配置大模型 API Key，使用基础提炼", progress=52)

    kmap_path = os.path.join(task_dir, "knowledge_map.json")
    with open(kmap_path, "w", encoding="utf-8") as f:
        json.dump(kmap, f, ensure_ascii=False, indent=2)

    notes_md = build_notes_md(kmap, srt_items, url, fmts)
    notes_path = os.path.join(task_dir, "notes.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(notes_md)

    update(4, "重点提炼完成，正在导出…", progress=70)
    files = {"notes.md": "notes.md", "knowledge_map.json": "knowledge_map.json"}

    if "pdf" in fmts:
        pdf_path = os.path.join(task_dir, "notes.pdf")
        update(5, "正在生成 PDF…", progress=78)
        ok, msg = export_pdf(notes_path, pdf_path, img_base=task_dir)
        _log("export_pdf ok=%s msg=%s" % (ok, msg[:200]))
        if ok and os.path.exists(pdf_path):
            files["笔记.pdf"] = "notes.pdf"

    if "mindmap" in fmts:
        mm_path = os.path.join(task_dir, "mindmap.png")
        update(6, "正在生成思维导图…", progress=88)
        ok, msg = gen_mindmap(kmap_path, mm_path)
        _log("gen_mindmap ok=%s msg=%s" % (ok, msg[:200]))
        if ok and os.path.exists(mm_path):
            files["思维导图.png"] = "mindmap.png"

    # 视频关键帧截图（安装 playwright 后自动启用）
    if type_info["key"] in ("bilibili", "youtube") and kmap.get("chapters"):
        if playwright_available():
            update(7, "正在截取重点画面…（playwright）", progress=94)
            pts = []
            for ch in kmap.get("chapters", []):
                for p in ch.get("points", []):
                    if p.get("start_sec"):
                        pts.append(str(int(p["start_sec"])))
            if pts:
                shot_dir = os.path.join(task_dir, "screenshots")
                os.makedirs(shot_dir, exist_ok=True)
                script = os.path.join(SCRIPTS, "screenshot_keyframes.py")
                ok = _run_script([sys.executable, script, url,
                                  "--points", ",".join(pts[:10]),
                                  "--out-dir", shot_dir, "--wait", "2"], timeout=180)
                _log("screenshot ok=%s" % ok)
                if ok:
                    n = len([f for f in os.listdir(shot_dir) if f.endswith(".png")])
                    if n:
                        files["重点截图"] = "screenshots/"
        else:
            _log("playwright 未安装，跳过截图")

    # Anki 卡片（勾选了即生成）
    if "anki" in fmts:
        anki_path = os.path.join(task_dir, "anki_cards.txt")
        update(7, "正在生成 Anki 卡片…", progress=94)
        if export_anki(kmap_path, anki_path):
            files["Anki卡片.txt"] = "anki_cards.txt"

    update(7, "整理完成！" + ("（含 AI 通俗解释）" if llm_on else ""), done=True, progress=100, result={
        "title": kmap.get("course_title", "学习笔记"),
        "files": files,
        "points": sum(len(c.get("points", [])) for c in kmap.get("chapters", [])),
        "chapters": len(kmap.get("chapters", [])),
        "llm_enhanced": bool(kmap.get("llm_enhanced"))
    })


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/process":
            body = self._read_body()
            if body is None:
                return
            url = (body.get("url") or "").strip()
            fmts = body.get("fmts") or ["md", "pdf", "mindmap"]
            if not url:
                self._json(400, {"error": "请提供资源链接或内容"})
                return
            type_info = detect_type(url)
            if not type_info:
                self._json(400, {"error": "无法识别资源类型"})
                return
            task_id = "t" + uuid.uuid4().hex[:12]
            with _lock:
                _tasks[task_id] = {
                    "id": task_id, "step": 0, "log": "已创建任务",
                    "done": False, "ts": time.time(),
                    "url": url[:200], "type": type_info["name"],
                    "typeKey": type_info["key"], "fmts": fmts
                }
            t = threading.Thread(target=process_task, args=(task_id, url, fmts, type_info), daemon=True)
            t.start()
            self._json(200, {"task_id": task_id, "type": type_info})
            return
        if path == "/api/export/anki":
            # 按需生成 Anki 卡片：POST {"id": <任务ID>}
            body = self._read_body()
            if body is None:
                return
            tid = (body.get("id") or "").strip()
            if ".." in tid or not tid:
                self._json(400, {"error": "无效任务 ID"})
                return
            task_dir = os.path.join(TASKS_DIR, tid)
            kmap_path = os.path.join(task_dir, "knowledge_map.json")
            if not os.path.exists(kmap_path):
                self._json(404, {"error": "笔记不存在（仅支持已完成的正式整理任务）"})
                return
            anki_path = os.path.join(task_dir, "anki_cards.txt")
            if export_anki(kmap_path, anki_path) and os.path.exists(anki_path):
                with _lock:
                    t = _tasks.get(tid)
                    if t and t.get("result") and t["result"].get("files") is not None:
                        t["result"]["files"]["Anki卡片.txt"] = "anki_cards.txt"
                self._json(200, {"ok": True, "file": "anki_cards.txt", "name": "Anki卡片.txt"})
            else:
                self._json(500, {"error": "Anki 卡片生成失败，请查看后端日志"})
            return
        if path == "/api/export/obsidian":
            # 写入 Obsidian Vault：POST {"id": <任务ID>, "vault": <路径>, "folder": <目录>}
            body = self._read_body()
            if body is None:
                return
            tid = (body.get("id") or "").strip()
            vault = (body.get("vault") or "").strip()
            folder = (body.get("folder") or "ZhixueNotes").strip()
            if ".." in tid or not tid:
                self._json(400, {"error": "无效任务 ID"})
                return
            if not vault:
                self._json(400, {"error": "请提供 Obsidian Vault 路径"})
                return
            task_dir = os.path.join(TASKS_DIR, tid)
            if not os.path.exists(os.path.join(task_dir, "notes.md")):
                self._json(404, {"error": "笔记不存在（仅支持已完成的正式整理任务）"})
                return
            ok = export_obsidian(task_dir, vault, folder)
            _log("export_obsidian ok=%s vault=%s" % (ok, vault))
            if ok:
                self._json(200, {"ok": True, "vault": vault, "folder": folder})
            else:
                self._json(500, {"error": "写入 Obsidian 失败，请检查 Vault 路径是否正确"})
            return
        self._json(404, {"error": "未知接口"})

    def _read_body(self):
        """读取并解析 POST body JSON；解析失败时返回 400 并返回 None"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") or "{}"
            return json.loads(raw)
        except Exception:
            self._json(400, {"error": "无效 JSON"})
            return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/status":
            tid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            with _lock:
                task = _tasks.get(tid)
            if not task:
                self._json(404, {"error": "任务不存在"})
                return
            self._json(200, task)
            return
        if path.startswith("/api/files/"):
            parts = path.split("/")
            if len(parts) == 5:
                tid, fname = parts[3], urllib.parse.unquote(parts[4])
                fpath = os.path.join(TASKS_DIR, tid, fname)
                if os.path.exists(fpath) and ".." not in fname:
                    self.send_response(200)
                    self._cors()
                    ct = "application/octet-stream"
                    if fname.endswith(".pdf"):
                        ct = "application/pdf"
                    elif fname.endswith(".png"):
                        ct = "image/png"
                    elif fname.endswith(".md"):
                        ct = "text/markdown; charset=utf-8"
                    elif fname.endswith(".json"):
                        ct = "application/json; charset=utf-8"
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
                    with open(fpath, "rb") as f:
                        self.end_headers()
                        self.wfile.write(f.read())
                    return
            self._json(404, {"error": "文件不存在"})
            return
        if path == "/api/notes":
            result = []
            with _lock:
                for tid, task in _tasks.items():
                    if task.get("done") and task.get("result"):
                        result.append({
                            "id": tid, "title": task["result"].get("title", "笔记"),
                            "type": task.get("type", ""), "typeKey": task.get("typeKey", ""),
                            "fmts": task.get("fmts", []), "time": task.get("ts", 0),
                            "files": task["result"].get("files", {}),
                            "points": task["result"].get("points", 0),
                            "chapters": task["result"].get("chapters", 0)
                        })
            result.sort(key=lambda x: x["time"], reverse=True)
            self._json(200, {"notes": result})
            return
        if path == "/api/read":
            tid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            fname = urllib.parse.parse_qs(parsed.query).get("file", ["notes.md"])[0]
            fpath = os.path.join(TASKS_DIR, tid, fname)
            if os.path.exists(fpath) and ".." not in fname:
                with open(fpath, "r", encoding="utf-8") as f:
                    self._json(200, {"content": f.read()})
                return
            self._json(404, {"error": "文件不存在"})
            return
        self._json(404, {"error": "未知接口"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="智学笔记本地后端服务")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = http.server.HTTPServer((args.host, args.port), Handler)
    print("智学笔记后端服务已启动: http://%s:%d" % (args.host, args.port))
    print("API:")
    print("  POST /api/process    启动整理任务")
    print("  GET  /api/status?id= 查询任务状态")
    print("  GET  /api/files/{id}/{filename}  下载结果文件")
    print("  GET  /api/notes      列出已完成笔记")
    print("  GET  /api/read?id=&file=  读取文件内容")
    print("  POST /api/export/anki       按需生成 Anki 卡片")
    print("  POST /api/export/obsidian   按需写入 Obsidian Vault")
    print("脚本目录: %s" % SCRIPTS)
    print("任务目录: %s" % TASKS_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()