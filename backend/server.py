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

# 技能脚本目录
_cfg = os.environ.get("TELEAGENT_CONFIG_DIR", os.path.expanduser("~/.config/TeleAgent"))
# TELEAGENT_CONFIG_DIR 已含完整用户路径（如 .../TeleAgent/users/v1_xxx）
# 若末尾不含 users/，则补上默认用户段；若已含则直接用
if "users" not in _cfg:
    _cfg = os.path.join(_cfg, "users", "v1_public_2102373803160674304")
SKILL_DIR = os.path.join(_cfg, "skills", "video-note-master")
SCRIPTS = os.path.join(SKILL_DIR, "scripts")

# 工作目录
WORK_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "TeleAgent",
                        "TeleAgent的工作空间")
TASKS_DIR = os.path.join(WORK_DIR, ".temp", "zhixue-tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# 任务状态
_tasks = {}
_lock = threading.Lock()

LOG_FILE = os.path.join(TASKS_DIR, "..", "zhixue-backend.log")

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
    try:
        proc = subprocess.Popen(
            [sys.executable, script, url, "--out-dir", out_dir, "--fmt", "srt"],
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


def basic_extract(items, title="学习资源", max_points=10):
    if not items:
        return {"course_title": title, "chapters": []}
    duration = items[-1][0] if items else 0
    seg_len = 300
    segments = []
    cur = []
    cur_start = items[0][0]
    for sec, text in items:
        if sec - cur_start > seg_len and cur:
            segments.append((cur_start, cur))
            cur = []
            cur_start = sec
        cur.append((sec, text))
    if cur:
        segments.append((cur_start, cur))

    chapters = []
    for i, (start, seg_items) in enumerate(segments[:max_points]):
        texts = [t for _, t in seg_items]
        full = " ".join(texts)
        sentences = re.split(r"[。！？.!?,，,]", full)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 8]

        def info_score(s):
            return len(re.findall(r"\d|[A-Z]{2,}", s)) + min(len(s), 40) / 40
        sentences.sort(key=info_score, reverse=True)
        title_text = sentences[0][:40] if sentences else "知识点 %d" % (i + 1)
        summary = " ".join(sentences[:3])[:120] if sentences else full[:120]
        chapters.append({
            "title": "第%d节 %s" % (i + 1, tcs(start)),
            "points": [{
                "title": title_text,
                "start_sec": int(start),
                "start_label": tcs(start),
                "summary": summary
            }]
        })
    return {"course_title": title, "duration_sec": int(duration), "chapters": chapters}


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
        lines.append("本笔记由智学笔记平台自动整理，涵盖以下核心内容。")
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
            if srt_items and p.get("start_sec"):
                nearby = [t for s, t in srt_items if abs(s - p["start_sec"]) < 60]
                if nearby:
                    lines.append("**原文摘录**：")
                    for t in nearby[:3]:
                        lines.append("> %s" % t)
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

    def update(step, log, done=False, result=None, error=None):
        with _lock:
            _tasks[task_id].update({
                "step": step, "log": log, "done": done,
                "result": result, "error": error, "ts": time.time()
            })

    title_prefix = type_info["name"]
    update(0, "正在识别资源类型…")
    update(1, "已识别：%s" % type_info["name"])

    srt_path = None
    srt_items = []
    if type_info["key"] in ("bilibili", "youtube"):
        update(2, "正在抓取字幕…（调用 fetch_subtitles.py）")
        ok, srt_path, msg = fetch_subtitles(url, task_dir, type_info["key"])
        if ok:
            srt_items = parse_srt(srt_path)
            update(3, "字幕抓取成功，共 %d 条，正在提炼重点…" % len(srt_items))
        else:
            update(3, "字幕抓取失败：%s，将基于文本处理" % msg[:100])
            srt_path = None
    elif type_info["key"] == "text":
        srt_items = [(0, url)]
        update(3, "文本内容已接收，正在提炼重点…")
    else:
        update(3, "该资源类型暂不支持自动抓取，请使用 TeleAgent 助手处理")

    kmap = basic_extract(srt_items, title="%s整理笔记" % title_prefix)

    # LLM 增强（配置了 API Key 时自动启用）
    llm_on = llm_available()
    if llm_on and srt_items:
        update(4, "大模型增强中：通俗解释 + 考点提炼…")
        excerpt = " ".join(t for _, t in srt_items)[:4000]
        enhanced = llm_enhance_note(kmap, transcript_excerpt=excerpt, source_url=url)
        if enhanced:
            kmap = enhanced
        summary = llm_generate_summary(excerpt, url)
        if summary:
            kmap["llm_summary"] = summary
    else:
        update(4, "未配置大模型 API Key，使用基础提炼")

    kmap_path = os.path.join(task_dir, "knowledge_map.json")
    with open(kmap_path, "w", encoding="utf-8") as f:
        json.dump(kmap, f, ensure_ascii=False, indent=2)

    notes_md = build_notes_md(kmap, srt_items, url, fmts)
    notes_path = os.path.join(task_dir, "notes.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(notes_md)

    update(4, "重点提炼完成，正在导出…")
    files = {"notes.md": "notes.md", "knowledge_map.json": "knowledge_map.json"}

    if "pdf" in fmts:
        pdf_path = os.path.join(task_dir, "notes.pdf")
        update(5, "正在生成 PDF…")
        ok, msg = export_pdf(notes_path, pdf_path, img_base=task_dir)
        _log("export_pdf ok=%s msg=%s" % (ok, msg[:200]))
        if ok and os.path.exists(pdf_path):
            files["笔记.pdf"] = "notes.pdf"

    if "mindmap" in fmts:
        mm_path = os.path.join(task_dir, "mindmap.png")
        update(6, "正在生成思维导图…")
        ok, msg = gen_mindmap(kmap_path, mm_path)
        _log("gen_mindmap ok=%s msg=%s" % (ok, msg[:200]))
        if ok and os.path.exists(mm_path):
            files["思维导图.png"] = "mindmap.png"

    update(7, "整理完成！" + ("（含 AI 通俗解释）" if llm_on else ""), done=True, result={
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
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(body)
            except Exception:
                self._json(400, {"error": "无效 JSON"})
                return
            url = (data.get("url") or "").strip()
            fmts = data.get("fmts") or ["md", "pdf", "mindmap"]
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
        self._json(404, {"error": "未知接口"})

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
    print("任务目录: %s" % TASKS_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()