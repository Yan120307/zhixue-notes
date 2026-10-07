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
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm import (llm_available, llm_enhance_note, llm_generate_summary,
                 llm_build_knowledge_map, test_connection, provider_info)
import config as cfgmod
import netutil
import docextract

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


_URL_RE = re.compile(r"https?://\S+", re.I)


def _strip_url(u):
    return u.rstrip(".,;:!?)]}\"'】） “").strip()


def detect_type(text):
    """
    识别任意输入：
    - 嵌在任何文本里的视频/网盘链接都能提取出来（如网盘分享消息）
    - 任意其他 URL 且以链接为主 → 网页抓取
    - 长文本仅引用 URL → 按文本内容处理
    """
    text = (text or "").strip()
    if not text:
        return None
    urls = [_strip_url(m.group(0)) for m in _URL_RE.finditer(text)]

    if urls:
        # 1) 视频站链接优先（纯链接或分享消息里嵌入的都算）
        for u in urls:
            if re.search(r"bilibili\.com|b23\.tv", u, re.I):
                return {"key": "bilibili", "name": "B站视频", "url": u}
            if re.search(r"youtube\.com|youtu\.be", u, re.I):
                return {"key": "youtube", "name": "YouTube 视频", "url": u}
        # 2) 网盘链接（常见云盘 / pan. 前缀 / /share/ 路径 / 校园网盘）
        for u in urls:
            if re.search(r"pan\.|aliyundrive|alipan|quark|123pan|lanzou|/share/|nwafu", u, re.I):
                return {"key": "pan", "name": "网盘分享", "url": u}
        # 3) PDF / 文档直链
        for u in urls:
            if re.search(r"\.pdf(\?|$)", u, re.I):
                return {"key": "pdf", "name": "PDF 文档", "url": u}
            if re.search(r"\.(docx?|pptx?|md|txt)(\?|$)", u, re.I):
                return {"key": "doc", "name": "文档资料", "url": u}
        # 4) 其余 URL：输入以链接为主（去掉 URL 后没剩多少字）→ 当网页抓取
        rest = _URL_RE.sub("", text).strip(" ，。；、:：!！?？（）()【】】")
        if len(rest) < 80:
            return {"key": "web", "name": "网页文章", "url": urls[0]}
        # 5) 长文本里仅引用了 URL → 按文本内容处理
    return {"key": "text", "name": "文本内容"}


def cleanup_tasks(force=False):
    """
    清理任务数据，返回 {"removed": [tid...], "freed_mb": x, "kept": n}。

    清理规则（都可在 ~/.zhixue-notes/config.json 的 cleanup 段调整）：
      - 不动正在进行中的任务（没有 done 状态且在近期创建）
      - 保留最近 keep_recent 个已完成任务
      - 保留 max_age_days 天内完成的
      - 总占用超过 max_total_mb 时，从最旧的开始删，直到降下来
      - 目录下没有任何结果文件的任务直接删（失败/中断留下的空壳）

    旧版没有任何清理：任务目录只增不减，长期使用会占满磁盘、内存里的
    _tasks 字典也会一直变大。
    """
    cfg = cfgmod.load_config()
    c = cfg.get("cleanup") or {}
    keep_recent = int(c.get("keep_recent", 50))
    max_age_days = int(c.get("max_age_days", 30))
    max_total_mb = int(c.get("max_total_mb", 3000))

    removed, freed = [], 0
    if not os.path.isdir(TASKS_DIR):
        return {"removed": removed, "freed_mb": 0, "kept": 0}

    now = time.time()
    entries = []
    for tid in os.listdir(TASKS_DIR):
        tdir = os.path.join(TASKS_DIR, tid)
        if not os.path.isdir(tdir):
            continue
        size, mtime = 0, now
        try:
            for root_dir, _dirs, files in os.walk(tdir):
                for fn in files:
                    try:
                        size += os.path.getsize(os.path.join(root_dir, fn))
                    except OSError:
                        pass
            mtime = os.path.getmtime(tdir)
        except OSError:
            pass
        has_result = os.path.isfile(os.path.join(tdir, "notes.md")) or \
                     os.path.isfile(os.path.join(tdir, "knowledge_map.json"))
        entries.append({"id": tid, "dir": tdir, "size": size,
                        "mtime": mtime, "has_result": has_result})

    def is_running(e):
        with _lock:
            t = _tasks.get(e["id"])
        return bool(t and not t.get("done") and (now - t.get("ts", now)) < 3600)

    # 删除顺序：先空壳，再最旧
    entries.sort(key=lambda e: (e["has_result"], e["mtime"]))
    total_mb = sum(e["size"] for e in entries) / 1048576.0
    finished = [e for e in entries if e["has_result"]]

    def do_remove(e):
        nonlocal total_mb, freed
        try:
            shutil.rmtree(e["dir"], ignore_errors=True)
        except Exception:
            return False
        removed.append(e["id"])
        freed += e["size"]
        total_mb -= e["size"] / 1048576.0
        with _lock:
            _tasks.pop(e["id"], None)
        return True

    # force=True 表示用户主动清理：除「正在跑的任务」外全删
    for e in entries:
        if is_running(e):
            continue
        if force:
            do_remove(e)
            continue
        age_days = (now - e["mtime"]) / 86400.0
        too_old = age_days > max_age_days
        over_count = (e["has_result"] and len(finished) - len(removed) > keep_recent)
        over_size = total_mb > max_total_mb
        if (not e["has_result"]) or too_old or over_count or over_size:
            do_remove(e)

    kept = 0
    if os.path.isdir(TASKS_DIR):
        kept = sum(1 for tid in os.listdir(TASKS_DIR)
                   if os.path.isdir(os.path.join(TASKS_DIR, tid)))
    if removed:
        _log("清理任务 %d 个，释放 %.1f MB" % (len(removed), freed / 1048576.0))
    return {"removed": removed, "freed_mb": round(freed / 1048576.0, 1),
            "kept": kept, "total_mb": round(total_mb, 1)}


def _cleanup_loop():
    """后台定时清理（每 6 小时一次，启动 5 分钟后先跑一次）"""
    time.sleep(300)
    while True:
        try:
            cleanup_tasks()
        except Exception as e:
            _log("定期清理异常: %s" % str(e)[:200])
        time.sleep(6 * 3600)


def _safe_path(tid, fname):
    """
    安全拼接任务目录内的文件路径。
    旧版用 `".." not in fname` 判断，绕不过 `..\\`、绝对路径、符号链接等情况；
    这里用 realpath 归一化后判断是否真在任务目录内。
    """
    try:
        base = os.path.realpath(TASKS_DIR)
        target = os.path.realpath(os.path.join(base, tid, fname))
        if target != base and not target.startswith(base + os.sep):
            return None
        return target
    except Exception:
        return None


def _load_task_index():
    """
    扫描磁盘上的任务目录，重建已完成任务列表（供服务重启后显示历史笔记）。
    只读每个任务的 knowledge_map.json 摘要与 notes.md 是否存在，成本很低。
    """
    out = []
    if not os.path.isdir(TASKS_DIR):
        return out
    for tid in os.listdir(TASKS_DIR):
        tdir = os.path.join(TASKS_DIR, tid)
        kmap_path = os.path.join(tdir, "knowledge_map.json")
        notes_path = os.path.join(tdir, "notes.md")
        if not (os.path.isfile(kmap_path) and os.path.isfile(notes_path)):
            continue
        try:
            with open(kmap_path, "r", encoding="utf-8") as f:
                kmap = json.load(f)
            files = {"notes.md": "notes.md", "knowledge_map.json": "knowledge_map.json"}
            for fname, label in (("notes.pdf", "笔记.pdf"), ("mindmap.png", "思维导图.png"),
                                 ("anki_cards.txt", "Anki卡片.txt")):
                if os.path.isfile(os.path.join(tdir, fname)):
                    files[label] = fname
            out.append({
                "id": tid,
                "title": kmap.get("course_title", "学习笔记"),
                "type": kmap.get("source_type", ""),
                "typeKey": kmap.get("source_type_key", ""),
                "src": kmap.get("source_url", ""),
                "fmts": kmap.get("fmts") or [],
                "time": kmap.get("created_at") or os.path.getmtime(kmap_path),
                "files": files,
                "points": sum(len(c.get("points", [])) for c in kmap.get("chapters", [])),
                "chapters": len(kmap.get("chapters", [])),
                "llm": bool(kmap.get("llm_summary") or kmap.get("llm_enhanced")
                            or kmap.get("llm_generated")),
                "from_disk": True,
            })
        except Exception:
            continue
    return out


def _public_task(t):
    """任务的可序列化视图（剔除线程同步对象）"""
    return {k: v for k, v in t.items() if k not in ("confirm_event",)}


def fetch_subtitles(url, out_dir, type_key, cid=None):
    if type_key not in ("bilibili", "youtube"):
        return False, None, "非视频资源，跳过字幕抓取"
    script = os.path.join(SCRIPTS, "fetch_subtitles.py")
    if not os.path.exists(script):
        return False, None, "字幕抓取脚本不存在"
    meta_path = os.path.join(out_dir, "video_meta.json")
    cmd = [sys.executable, script, url, "--out-dir", out_dir, "--fmt", "srt",
           "--meta-out", meta_path]
    # 多 P 视频：把已解析出的 cid 传下去，否则永远抓第一 P（用户点 ?p=5 也没用）
    if cid:
        cmd += ["--cid", str(cid)]
    try:
        proc = subprocess.Popen(
            cmd,
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


# ---------------------------------------------------------------------------
# 启发式提炼的标题生成（旧版直接把句子截前 18/36 个字当标题，
# 结果标题是半截话，笔记看起来就是「乱总结」。这里改为真正提炼标题。）
# ---------------------------------------------------------------------------
# 口语填充词：出现在句首时对标题没有信息量，去掉。
# 注意顺序：长词必须排在短词前面，否则「这个就是」会先被「就是」吃掉，留下怪标题。
_FILLER_HEAD = re.compile(
    r"^(这个就是|那个就是|也就是说|举个例子|比如说|顾名思义|换句话说|"
    r"那么|然后|所以|因此|但是|不过|其实|这个|那个|我们|你们|大家|"
    r"接下来|首先|其次|最后|另外|还有|"
    r"好|嗯|啊|呃|OK|ok)[，,、\s]*")

# 过渡/寒暄句：整句没有知识点，直接丢弃（而不是剥掉词头留半句）
_TRANSITION = re.compile(
    r"^(那么|然后|所以|因此|但是|不过|接下来|首先|其次|最后|另外|还有|"
    r"大家好|同学们|我们来看|我们来看一下|我们讲|我们看一下|下面|"
    r"这节课|本节课|上一节|好了)")
# 遇到这些就说明这句在讲"课程安排"而不是知识本身
_META_SENTENCE = re.compile(r"(我们来看|我们讲一下|我们看一下|下面开始|接下来讲|这节课我们)")

# 概念性词头（常见学科名词结尾）：截断后不该把这类词切成两半
_CONCEPT_HEAD = re.compile(
    r"[\u4e00-\u9fa5](率|法|度|值|量|数|器|式|项|性|化|点|线|面|体|群|环|域|集|表|图|树|"
    r"积|和|差|商|幂|根|角|边|函数|变量|矩阵|向量|集合|模型|网络|算法|系统|规则|公式)$")
# 程度副词/否定：出现在概念名后面说明那里该断句
_DEGREE = re.compile(r"(过|越|太|很|最|更|不|非常|特别|相当|比较|稍微)")

# 谓语标记：短语里出现这些说明它是句子而非概念名，应从谓语前截断
_PREDICATE = re.compile(
    r"(指的是|是指|叫做|称为|是一种|是一个|需要|可以|能够|应该|必须|就是|属于|"
    r"通过|用来|用于|决定|实现|表示|代表|导致|造成|产生|提供|支持|出现|发生|获得|"
    r"得到|完成|提高|降低|提升|减少|增加|影响|解决|处理|执行|调用|返回|输出|输入|"
    r"保存|读取|写入|计算|比较|判断|选择|分为|包括|包含|组成|基于|依赖|随着|"
    r"对于|关于|如果|因为|所以|但是|并且|而且|会|能|要|是|由|把|让|使得|变|越)")

# 概念性词尾：截断点正好切断它时，应把词尾一起保留（"反向传播的原理" ≠ "反向传播"）
_CONCEPT_TAIL = re.compile(
    r"(原理|机制|流程|步骤|方法|算法|公式|定理|定义|概念|结构|模型|"
    r"区别|对比|作用|用途|特点|优点|缺点|条件|规则|规范|技巧|思路|"
    r"问题|错误|注意事项|总结|分类|类型|参数|配置|接口|函数|"
    r"命令|语法|格式|标准|协议|架构|框架|工具|环境|依赖|方式|"
    r"核心|关键|重点|本质|特性|性质|关系|过程|阶段|环节|场景)$")


def make_title(sentence, max_len=24):
    """
    从一个句子提炼知识点标题。

    关键思路（旧版是把句子截前 36 字，所以标题是半截话）：
      概念名通常是「名词短语」，而完整句子带谓语。
      因此：优先取判断句主语 / 冒号前 / 引号内术语；
      再退到「去掉句尾标点后是否是短名词短语」；
      最后才在第一个谓语处截断，并保证不切断概念性词尾。
    """
    s = re.sub(r"\s+", "", str(sentence or "").strip())
    if not s:
        return ""
    # 纯过渡句/寒暄句不产生知识点
    if _TRANSITION.match(s) and _META_SENTENCE.search(s):
        return ""
    s = _FILLER_HEAD.sub("", s)

    # 1) 判断句取主语："A 是/指的是/称为 B"
    m = re.match(r"^(.{2,%d}?)(?:指的是|是指|叫做|称为|是一种|是一个|是)" % max_len, s)
    if m:
        cand = _clean_title(m.group(1))
        if len(cand) >= 3:
            return cand

    # 2) 冒号前（太短则不可靠，继续往下走）
    m = re.match(r"^(.{3,%d}?)[：:]" % max_len, s)
    if m:
        cand = _clean_title(m.group(1))
        if len(cand) >= 3:
            return cand
    # 2b) 「注意：」「重点：」这类模态前缀没有信息量，剥掉后用冒号后的内容继续提炼
    m = re.match(r"^(注意|重点|关键|提示|总结|结论|补充|举例|说明)[：:]", s)
    if m:
        s = s[m.end():]
        if not s:
            return ""
        m2 = re.match(r"^(.{2,%d}?)(?:指的是|是指|叫做|称为|是一种|是一个|是)" % max_len, s)
        if m2:
            cand = _clean_title(m2.group(1))
            if len(cand) >= 3:
                return cand

    # 3) 引号/书名号里的术语（一律用 Unicode 转义，避免引号嵌套带来语法问题）
    m = re.search("\u300a\u300c\u300e([^\u300b\u300d\u300f]{2,%d})"
                  "\u300b\u300d\u300f" % max_len, s)
    if m:
        return _clean_title(m.group(1))

    # 4) 去掉句尾标点后已经够短 → 它本身就是个名词短语
    body = s.rstrip("。！？；，,、!?;")
    if 2 <= len(body) <= max_len:
        return _clean_title(body)

    # 5) 在第一个谓语处截断
    cut = max_len
    m = _PREDICATE.search(s)
    if m and 3 <= m.start() <= cut:
        cut = m.start()
        tail = _CONCEPT_TAIL.search(s[:cut])       # 谓语前有概念词尾则一起保留
        if tail and tail.start() >= 2:
            cut = tail.end()
    piece = s[:cut]

    # 6) 概念名后面紧跟程度副词 → 从副词处断掉（"学习率过大" -> "学习率"）
    d = _DEGREE.search(piece)
    if d and d.start() >= 2:
        candidate = piece[:d.start()]
        if _CONCEPT_HEAD.search(candidate):
            piece = candidate

    # 7) 尽量落在概念词尾上
    tail = _CONCEPT_TAIL.search(piece)
    if tail and tail.start() >= 2:
        piece = piece[:tail.end()]

    # 8) 仍然太长时，退到最后一个概念性词头
    piece = _clean_title(piece)
    if len(piece) > 18:
        for i in range(len(piece), 2, -1):
            if _CONCEPT_HEAD.search(piece[:i]):
                piece = piece[:i]
                break
    return _clean_title(piece)


def _clean_title(t):
    """去掉标题首尾的标点与语气虚词"""
    t = re.sub(r"^[，,、。；;：:\s]+", "", str(t or ""))
    t = re.sub(r"[，,、。；;：:\s]+$", "", t)
    t = re.sub(r"(的是|的了|了呢|的吗|的话)$", "", t)
    if len(t) > 24:
        t = t[:24]
    return t.strip()


def keyword_scores(texts, top_n=60):
    """
    中文轻量关键词抽取：按 2~4 字高频片段打分。
    纯标准库实现（不引入 jieba），用于给句子加权、判断知识点含金量。
    """
    freq = {}
    for t in texts:
        # 只看中文片段，按标点切开，避免跨标点的假词
        for seg in re.split(r"[^\u4e00-\u9fa5]+", t):
            for n in (2, 3, 4):
                for i in range(len(seg) - n + 1):
                    w = seg[i:i + n]
                    freq[w] = freq.get(w, 0) + 1
    # 去掉被更长词包含的短词（例如同时有「函数」和「函数定义」时留长的）
    items = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    picked = []
    for w, c in items:
        if c < 2:
            continue
        if any(w in p and w != p for p in picked):
            continue
        picked.append(w)
        if len(picked) >= top_n:
            break
    return {w: i for i, w in enumerate(picked)}


def key_sentence_score(sentence, kws):
    """句子得分 = 关键词覆盖 + 信息量（数值/术语） - 口语化惩罚"""
    score = info_score(sentence)
    hit = 0
    for w in kws:
        if w in sentence:
            hit += 1
    score += hit * 1.5
    if _FILLER_HEAD.match(sentence):
        score -= 0.8                     # 满口"然后/那么"的多半是废话
    if len(sentence) < 10:
        score -= 1.0
    return score


def _find_excerpt(sent, texts):
    """在原始条目里找包含要点句的原文，返回该条目及相邻上下文（保序去重）"""
    key = sent[:10]
    for i, t in enumerate(texts):
        if key and key in t:
            out = [texts[max(0, i - 1)], t]
            if i + 1 < len(texts):
                out.append(texts[i + 1])
            break
    else:
        out = texts[:2]
    # 保序去重
    seen, uniq = set(), []
    for t in out:
        if t not in seen and len(t.strip()) >= 6:
            seen.add(t)
            uniq.append(t)
    return uniq or texts[:1]


def _dedup_items(items):
    """相邻重复条目合并（转写/字幕常见同句重复，避免摘录和要点重复）"""
    out = []
    last = None
    for sec, text in items:
        t = text.strip()
        if last is not None and (t == last or (len(t) >= 10 and t in last) or (len(last) >= 10 and last in t)):
            continue
        out.append((sec, text))
        last = t
    return out


def basic_extract(items, title="学习资源", max_chapters=10):
    """
    从内容条目中提炼知识图谱（无大模型时的启发式兜底）。

    items: [(sec, text), ...] —— 有真实时间轴（视频字幕）时按 300 秒分章；
    无时间轴（网页段落/纯文本）时按条目数分章。

    相比旧版的改进：
      - 标题用 make_title() 真正提炼，不再是「句子截前 18 字」
      - 句子权重加入全局关键词覆盖度，挑出的点更接近真正知识点
      - 章节名由该章最高分要点概括，不再与首个要点标题重复
      - 摘要去重，不会出现三句一样的话
    """
    if not items:
        return {"course_title": title, "chapters": [], "has_timeline": False}

    items = _dedup_items(items)
    secs = [s for s, _ in items]
    has_timeline = (max(secs) - min(secs)) >= 120
    kmap = {"course_title": title, "chapters": [], "has_timeline": has_timeline}

    # 全局关键词表：让「哪句更有含金量」有依据，而不是只看长度
    all_texts = [t for _, t in items]
    kws = keyword_scores(all_texts)

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
            sentences = [full[:60]] if full.strip() else []
        ranked = sorted(sentences, key=lambda s: key_sentence_score(s, kws), reverse=True)

        n_points = max(1, min(3, len(sentences) // 4 + 1))
        points = []
        used_titles = set()
        used_sents = set()
        for sent in ranked[:n_points * 3]:
            if len(points) >= n_points:
                break
            point_title = make_title(sent)
            # 太短的标题（多是过渡句被剥完词头后的残渣）不可靠，丢掉换下一句
            if not point_title or len(point_title) < 4:
                continue
            title_key = re.sub(r"\s+", "", point_title)[:10]
            if title_key in used_titles:
                continue
            used_titles.add(title_key)
            used_sents.add(sent[:20])

            # 摘要 = 该要点句 + 章内另外 2 句补充（去重）
            summary_parts = [sent]
            for cand in ranked:
                if len(summary_parts) >= 3:
                    break
                if cand[:20] in used_sents or cand == sent:
                    continue
                summary_parts.append(cand)
                used_sents.add(cand[:20])
            points.append({
                "title": point_title,
                "start_sec": int(start) if has_timeline else None,
                "start_label": tcs(start) if has_timeline else None,
                "summary": " ".join(summary_parts)[:220],
                "excerpt": _find_excerpt(sent, texts),
            })

        if not points:
            continue

        # 章节名：用本章最高分要点的标题，比截断句子通顺
        chap_name = points[0]["title"]
        if len(chap_name) < 4 and ranked:
            chap_name = make_title(ranked[0]) or ("内容 %d" % (gi + 1))
        if has_timeline:
            chap_title = "第%d节 [%s] %s" % (gi + 1, tcs(start), chap_name)
        else:
            chap_title = "第%d节 %s" % (gi + 1, chap_name)
        chapters.append({"title": chap_title, "points": points})

    kmap["chapters"] = chapters[:max_chapters]
    return kmap


def build_knowledge_map(items, title="学习资源", progress=None):
    """
    统一的「内容条目 -> 知识图谱」入口。

    优先用大模型（质量高得多），失败或未配置时回落到启发式提炼。
    这样保证：配了模型质量更好，没配模型也能用，且永远不会因为模型抽风而产出空笔记。
    """
    from config import load_config
    ex = (load_config().get("extract") or {})
    max_chapters = int(ex.get("max_chapters") or 12)
    points_per_chapter = int(ex.get("points_per_chapter") or 5)

    if llm_available():
        try:
            quality = (load_config().get("llm") or {}).get("quality") or "standard"
            kmap = llm_build_knowledge_map(
                items, title=title, max_chapters=max_chapters,
                points_per_chapter=points_per_chapter, quality=quality,
                progress=progress)
            if kmap and kmap.get("chapters"):
                return kmap, True
            _log("LLM 提炼未返回有效结果，回落到启发式提炼")
        except Exception as e:
            _log("LLM 提炼异常，回落到启发式提炼: %s" % str(e)[:200])
    else:
        if progress:
            try:
                progress(4, "未配置大模型，使用基础提炼（可在设置页一键接入）")
            except Exception:
                pass

    return basic_extract(items, title=title, max_chapters=max_chapters), False


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


def _ask_confirm(task_id, msg, update, wait_sec=900):
    """
    重操作（音频下载/转写）前征求用户同意。
    返回 True=已同意，False=用户拒绝或超时。
    旧版把这段逻辑复制了两遍（视频一次、本地文件一次），现在统一到一处。
    """
    with _lock:
        _tasks[task_id]["need_confirm"] = {"message": msg}
    update(2, "等待确认：是否下载音频并本地转写？", progress=10)
    ev = _tasks[task_id].get("confirm_event")
    got = ev.wait(timeout=wait_sec) if ev else False
    with _lock:
        _tasks[task_id]["need_confirm"] = None
        agreed = _tasks[task_id].get("confirm_agree")
    return bool(got and agreed)


def acquire_content(task_id, url, type_info, task_dir, update):
    """
    按资源类型抓取/获取原始内容，统一返回：

        (items, title, duration, error)
        items: [(秒, 文本), ...]；视频类有真实秒数，网页/文本/文档为 0

    相比旧版：
      - 视频链接统一走 netutil.resolve_bilibili（短链 b23.tv、av 号、多 P ?p=N 全支持）
      - YouTube 没装 yt-dlp 时，如果有转写能力就用转写兜底（旧版直接失败）
      - PDF / Word / txt 直链新增自动解析（旧版直接说"不支持"）
      - 网页抓取失败时可用公共阅读器兜底（可关）
      - 所有分支都保留「复制文稿粘贴」这条保底建议
    """
    type_key = type_info.get("key")

    # ---------------- 视频类 ----------------
    if type_key in ("bilibili", "youtube"):
        return _acquire_video(task_id, url, type_key, task_dir, update)

    # ---------------- 网页文章 ----------------
    if type_key == "web":
        update(2, "正在抓取网页正文…", progress=12)
        ok, wtitle, paras, werr = fetch_webpage(url, task_dir)
        if ok and len(paras) >= 3:
            update(3, "正文抓取成功，共 %d 段，正在提炼重点…" % len(paras), progress=45)
            return [(0, p) for p in paras], (wtitle or None), 0, None

        # 主抓取失败 → 阅读器兜底（JS 渲染页面、反爬页面）
        if cfgmod.load_config().get("network", {}).get("reader_fallback", True):
            update(2, "常规抓取失败，尝试用公共阅读器兜底…", progress=20)
            rr = netutil.reader_fetch(url)
            if rr.get("ok"):
                rparas = netutil.split_paragraphs(rr.get("text") or "")
                if len(rparas) >= 3:
                    update(3, "阅读器兜底成功，共 %d 段，正在提炼重点…" % len(rparas),
                           progress=45)
                    return ([(0, p) for p in rparas],
                            (wtitle or rr.get("title") or None), 0, None)
            werr = (werr or "网页正文抓取失败") + "；阅读器兜底也未成功"
        return [], (wtitle or None), 0, (
            werr or "网页正文抓取失败") + "。建议：直接复制网页文字粘贴到输入框"

    # ---------------- 文档直链（PDF / Word / txt） ----------------
    if type_key in ("pdf", "doc"):
        update(2, "正在下载并解析文档…", progress=12)
        doc = docextract.fetch_document(url)
        if doc.get("ok") and len(doc.get("paragraphs") or []) >= 1:
            n = len(doc["paragraphs"])
            update(3, "文档解析成功（%s，%d 段），正在提炼重点…" % (
                doc.get("kind", "").upper(), n), progress=45)
            return ([(0, p) for p in doc["paragraphs"]],
                    doc.get("title") or "文档资料", 0, None)
        return [], doc.get("title") or None, 0, (
            doc.get("error") or "文档解析失败") + "。建议：把文档文字复制到输入框"

    # ---------------- 纯文本 ----------------
    if type_key == "text":
        sents = [s for s in split_sentences(url) if len(s.strip()) >= 6]
        if len(sents) < 3:
            return [], None, 0, "输入内容太短（有效句不足 3 句），请粘贴更完整的学习内容"
        update(3, "已接收 %d 句内容，正在提炼重点…" % len(sents), progress=45)
        return [(0, s) for s in sents], re.sub(r"\s+", " ", url.strip())[:24], 0, None

    # ---------------- 本地上传的音视频 ----------------
    if type_key == "local_video":
        return _acquire_local_media(task_id, url, type_info, task_dir, update)

    # ---------------- 网盘 ----------------
    if type_key == "pan":
        u = (url or "").lower()
        if "nwafu" in u or ".edu.cn" in u:
            return [], None, 0, (
                "已识别到校园网盘分享（该网盘在当前网络无法直接访问）。\n"
                "建议：① 下载视频后拖入平台，自动转写整理出完整笔记；"
                "② 复制视频文稿/字幕粘贴到输入框")
        return [], None, 0, (
            "已识别到网盘分享链接，网盘文件暂不支持自动下载。\n"
            "建议：① 下载后把视频/音频文件拖入平台整理；② 复制文稿粘贴到输入框")

    # ---------------- 兜底：任何没识别的链接都试着当网页抓 ----------------
    # 旧版这里直接返回「不支持」，导致大量没被规则命中的链接被判失败
    if re.search(r"^https?://", url or "", re.I):
        update(2, "未识别的链接类型，按网页文章尝试抓取…", progress=12)
        ok, wtitle, paras, werr = fetch_webpage(url, task_dir)
        if ok and len(paras) >= 3:
            update(3, "正文抓取成功，共 %d 段，正在提炼重点…" % len(paras), progress=45)
            return [(0, p) for p in paras], (wtitle or None), 0, None
        if cfgmod.load_config().get("network", {}).get("reader_fallback", True):
            rr = netutil.reader_fetch(url)
            if rr.get("ok"):
                rparas = netutil.split_paragraphs(rr.get("text") or "")
                if len(rparas) >= 3:
                    update(3, "阅读器兜底成功，共 %d 段…" % len(rparas), progress=45)
                    return ([(0, p) for p in rparas],
                            (wtitle or rr.get("title") or None), 0, None)
        return [], None, 0, ("这个链接抓不到可整理的内容（%s）。"
                             "建议：① 确认链接在浏览器里能正常打开；"
                             "② 复制页面文字粘贴到输入框" % (werr or "页面无正文"))

    return [], None, 0, "无法识别该资源类型，请粘贴链接、文本或拖入音视频文件"


def _acquire_video(task_id, url, type_key, task_dir, update):
    """视频类内容获取：字幕优先，无字幕则（征得同意后）本地转写"""
    update(2, "正在解析视频信息…", progress=10)
    meta = {}
    duration = 0
    title = None

    if type_key == "bilibili":
        # 关键修复：短链/av号/多P 统一在这里解析
        info = netutil.resolve_bilibili(url)
        if not info.get("ok"):
            return [], None, 0, info.get("error") or "B 站视频解析失败"
        meta = {
            "platform": "bilibili",
            "bvid": info["bvid"],
            "cid": info["cid"],
            "title": info.get("title") or "",
            "part": info.get("page_title") or "",
            "duration": info.get("duration") or 0,
            "page_index": info.get("page_index") or 1,
            "total_pages": info.get("total_pages") or 1,
        }
        title = info.get("title")
        duration = info.get("duration") or 0
        # 把解析结果落盘，供转写脚本复用（避免它再解析一次、也避免它拿错分P）
        _write_meta(task_dir, meta)
        if meta["total_pages"] > 1:
            update(2, "已识别视频：《%s》 第 %d/%d P" % (
                str(title or "")[:30], meta["page_index"], meta["total_pages"]),
                progress=11)

    update(2, "正在抓取字幕…", progress=12)
    # bilibili 走已解析的 cid（支持分 P）；youtube 不需要
    ok, srt_path, msg = fetch_subtitles(
        url, task_dir, type_key, cid=(meta.get("cid") if type_key == "bilibili" else None))
    if not meta:
        meta = _read_meta(task_dir) or {}
    title = title or meta.get("title")
    duration = duration or meta.get("duration") or 0

    if ok:
        srt_items = parse_srt(srt_path)
        if len(srt_items) < 8:
            return [], title, duration, (
                "该视频字幕内容过少（%d 条），无法整理出有效笔记" % len(srt_items))
        update(3, "字幕抓取成功，共 %d 条，正在提炼重点…" % len(srt_items), progress=45)
        return srt_items, title, duration, None

    # 无字幕：能转写就转写（bilibili 与 youtube 都试，旧版只对 bilibili 试）
    if faster_whisper_available():
        dur_sec = int(duration or 0)
        audio_mb = dur_sec / 123.0 if dur_sec else 0
        est_min = max(1, int(dur_sec / 180)) if dur_sec else 0
        confirm_msg = (
            "《%s》没有获取到字幕。\n\n"
            "将下载音频（约 %.0f MB）到本机，用本地模型转写（预计约 %d 分钟），"
            "全程不上传任何内容；笔记生成后音频文件会自动删除。\n\n是否继续？" % (
                str(title or "该视频")[:36], audio_mb, est_min))
        if not _ask_confirm(task_id, confirm_msg, update):
            return [], title, duration, (
                "已取消转写（或长时间未确认）。可换有字幕的视频，"
                "或复制文稿粘贴到输入框")

        update(2, "已确认，开始下载音频并本地转写…", progress=12)
        ok2, srt_path2, terr = transcribe_video(url, task_dir, update)
        if ok2:
            srt_items = parse_srt(srt_path2)
            if len(srt_items) < 8:
                return [], title, duration, (
                    "转写内容过少（%d 条），可能该视频无语音内容" % len(srt_items))
            update(3, "音频转写完成，共 %d 条，正在提炼重点…" % len(srt_items), progress=45)
            return srt_items, title, duration, None
        return [], title, duration, (
            "《%s》无字幕且本地转写失败：%s。可复制视频文稿粘贴到输入框" % (
                str(title or "该视频")[:30], str(terr)[:80]))

    # 没装转写能力：给出明确的安装指引
    if type_key == "bilibili":
        reason = ("该视频没有 CC 字幕（B站字幕需 UP 主上传），且本机未安装 faster-whisper。"
                  "安装后可自动转写音频：pip install faster-whisper；"
                  "或复制视频文稿粘贴到输入框")
    else:
        reason = ("未取到该视频的字幕（%s）。可执行 pip install yt-dlp 提升字幕抓取成功率，"
                  "或安装 faster-whisper 走本地转写；也可以复制文稿粘贴到输入框" % str(msg)[:60])
    if title:
        reason = "《%s》%s" % (str(title)[:40], reason)
    return [], title, duration, reason


def _acquire_local_media(task_id, src_path, type_info, task_dir, update):
    """本地上传的音视频：征得同意后本地转写（不涉及下载）"""
    fname = type_info.get("name") or os.path.basename(src_path)
    size_mb = os.path.getsize(src_path) / 1048576 if os.path.exists(src_path) else 0
    confirm_msg = (
        "将用本地模型转写「%s」（约 %.0f MB，预计约为时长的 1/3），"
        "全程不上传任何内容；笔记生成后源文件会自动删除。\n\n是否继续？" % (
            fname[:40], size_mb))
    if not _ask_confirm(task_id, confirm_msg, update):
        return [], None, 0, "已取消转写。"

    update(2, "已确认，开始本地转写…", progress=15)
    stop_flag = {"v": False}
    try:
        sys.path.insert(0, SCRIPTS)
        import transcribe_audio as _ta
        prog = os.path.join(task_dir, "transcode_progress.json")

        def _watch():
            last = ""
            while not stop_flag["v"]:
                try:
                    if os.path.exists(prog):
                        with open(prog, "r", encoding="utf-8") as f:
                            pr = json.load(f)
                        msg = pr.get("message") or ""
                        pct = min(100.0, float(pr.get("percent") or 0))
                        if msg != last:
                            last = msg
                            update(3, msg, progress=15 + pct * 0.30)
                except Exception:
                    pass
                time.sleep(2)

        threading.Thread(target=_watch, daemon=True).start()
        srt_out, n = _ta.transcribe(src_path, task_dir, None)
        stop_flag["v"] = True
        srt_items = parse_srt(srt_out)
        update(3, "转写完成，共 %d 条，正在提炼重点…" % len(srt_items), progress=45)
        if len(srt_items) < 8:
            return [], fname, 0, "转写内容过少（%d 条），可能该文件无语音内容" % len(srt_items)
        return srt_items, fname, 0, None
    except Exception as e:
        stop_flag["v"] = True
        return [], fname, 0, "本地转写失败：%s" % str(e)[:100]
    finally:
        stop_flag["v"] = True
        # 用户约定：转写后删除上传的源文件（原始文件仍在用户手中）
        try:
            if os.path.exists(src_path):
                os.remove(src_path)
        except Exception:
            pass


def _write_meta(task_dir, meta):
    """把视频元信息写入任务目录（供转写脚本与笔记标题复用）"""
    try:
        with open(os.path.join(task_dir, "video_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def process_task(task_id, url, fmts, type_info):
    task_dir = os.path.join(TASKS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    def update(step, log, done=False, result=None, error=None, progress=None):
        d = {"step": step, "log": log, "done": done,
             "result": result, "error": error, "ts": time.time()}
        if progress is not None:
            d["progress"] = progress
        with _lock:
            t = _tasks.get(task_id)
            if t is None:
                return
            t.update(d)

    title = None
    fail = None
    video_duration = 0

    update(0, "正在识别资源类型…", progress=3)
    update(1, "已识别：%s" % type_info["name"], progress=8)

    type_key = type_info["key"]

    # 统一走 acquire_content：视频/网页/文档/文本/网盘/本地文件 全在这里处理
    srt_items, title, video_duration, fail = acquire_content(
        task_id, url, type_info, task_dir, update)

    if fail:
        update(3, "整理失败：%s" % fail[:150], done=True, error=fail)
        return

    update(3, "内容获取完成（%d 条），正在提炼重点…" % len(srt_items), progress=48)

    # 提炼知识图谱：优先大模型，失败自动回落到启发式提炼
    kmap, used_llm = build_knowledge_map(
        srt_items, title=(title or "%s整理笔记" % type_info["name"]),
        progress=update)
    if type_key in ("bilibili", "youtube") and video_duration:
        kmap["duration_sec"] = int(video_duration)

    llm_on = llm_available()

    # 大模型已参与提炼时，通俗解释通常已在提炼阶段产出；
    # 只有「启发式提炼 + 有模型」的情况才需要单独补一遍通俗解释。
    if llm_on and srt_items and not kmap.get("llm_enhanced"):
        update(4, "大模型增强中：通俗解释 + 考点提炼…", progress=58)
        excerpt = " ".join(t for _, t in srt_items)[:12000]
        try:
            enhanced = llm_enhance_note(kmap, transcript_excerpt=excerpt, source_url=url)
            if enhanced:
                kmap = enhanced
        except Exception as e:
            _log("llm_enhance_note 异常: %s" % str(e)[:200])
    elif not llm_on:
        update(4, "未接入大模型，使用基础提炼（可在设置页一键接入）", progress=55)

    # 概述 + 必背考点（有模型才做，且失败不影响笔记产出）
    if llm_on and srt_items:
        try:
            summary = llm_generate_summary(" ".join(t for _, t in srt_items)[:12000], url)
            if summary:
                kmap["llm_summary"] = summary
        except Exception as e:
            _log("llm_generate_summary 异常: %s" % str(e)[:200])


    # 把「这份笔记是怎么来的」一并落盘：服务重启后从磁盘重建列表时，
    # 才能正确显示输出格式、来源类型与原始链接（旧版这些信息只在内存里）
    kmap["source_type"] = type_info.get("name", "")
    kmap["source_type_key"] = type_key
    kmap["source_url"] = (url or "")[:500]
    kmap["fmts"] = list(fmts or [])
    kmap["created_at"] = time.time()

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
            # 识别时若提取出了具体资源 URL（如从分享消息中提取），优先使用
            resource_url = type_info.pop("url", None) or url
            task_id = "t" + uuid.uuid4().hex[:12]
            with _lock:
                _tasks[task_id] = {
                    "id": task_id, "step": 0, "log": "已创建任务",
                    "done": False, "ts": time.time(),
                    "url": resource_url[:300], "type": type_info["name"],
                    "typeKey": type_info["key"], "fmts": fmts,
                    "confirm_event": threading.Event(),
                    "confirm_agree": None,
                    "need_confirm": None
                }
            t = threading.Thread(target=process_task, args=(task_id, resource_url, fmts, type_info), daemon=True)
            t.start()
            self._json(200, {"task_id": task_id, "type": type_info})
            return
        if path == "/api/task/confirm":
            # 转写等重操作前的用户确认：POST {"id": <任务ID>, "agree": true/false}
            body = self._read_body()
            if body is None:
                return
            tid = (body.get("id") or "").strip()
            agree = bool(body.get("agree"))
            with _lock:
                t = _tasks.get(tid)
                if not t:
                    self._json(404, {"error": "任务不存在"})
                    return
                t["confirm_agree"] = agree
                ev = t.get("confirm_event")
            if ev:
                ev.set()
            self._json(200, {"ok": True, "agree": agree})
            return
        if path == "/api/upload":
            # 本地音视频文件上传：multipart 解析，存任务目录后进入转写整理流程
            ctype = self.headers.get("Content-Type", "")
            m = re.search(r"boundary=(.+)", ctype)
            if not m:
                self._json(400, {"error": "无效的上传请求"})
                return
            boundary = ("--" + m.group(1).strip()).encode()
            length = int(self.headers.get("Content-Length", 0))
            if length > 1024 * 1024 * 1024:
                self._json(413, {"error": "文件超过 1GB 上限，请裁剪或压缩后重试"})
                return
            body = self.rfile.read(length)
            for part in body.split(boundary):
                if b"filename=" not in part:
                    continue
                header_blob, _, data = part.partition(b"\r\n\r\n")
                if data.endswith(b"\r\n"):
                    data = data[:-2]
                hm = re.search(rb'filename="([^"]+)"', header_blob)
                if not hm or not data:
                    continue
                fname = hm.group(1).decode("utf-8", "ignore")
                ext = os.path.splitext(fname)[1].lower()
                if ext not in (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", 
                               ".m4a", ".mp3", ".wav", ".aac", ".flac"):
                    self._json(400, {"error": "仅支持音视频文件（mp4/mkv/mov/mp3/wav 等）"})
                    return
                task_id = "t" + uuid.uuid4().hex[:12]
                task_dir = os.path.join(TASKS_DIR, task_id)
                os.makedirs(task_dir, exist_ok=True)
                src = os.path.join(task_dir, "source_media" + ext)
                with open(src, "wb") as f:
                    f.write(data)
                size_mb = len(data) / 1048576
                with _lock:
                    _tasks[task_id] = {
                        "id": task_id, "step": 0, "log": "文件已接收", "done": False,
                        "ts": time.time(), "url": fname[:300], "type": "视频/音频文件",
                        "typeKey": "local_video", "fmts": [],
                        "confirm_event": threading.Event(),
                        "confirm_agree": None, "need_confirm": None
                    }
                threading.Thread(target=process_task, daemon=True, args=(
                    task_id, src, ["md", "pdf", "mindmap"],
                    {"key": "local_video", "name": fname[:40]})).start()
                self._json(200, {"task_id": task_id, "filename": fname,
                                 "size_mb": round(size_mb, 1)})
                return
            self._json(400, {"error": "未找到上传文件"})
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
        if path == "/api/config/save":
            # 保存配置：body 形如
            # {"provider":"siliconflow","api_key":"sk-x","model":"...","enabled":true,
            #  "quality":"standard","base_url":"..."}
            body = self._read_body()
            if body is None:
                return
            provider = (body.get("provider") or "").strip() or None
            api_key = body.get("api_key")
            # 前端拿到的是打码值；用户没改动时原样回传，这里要认出并忽略
            if isinstance(api_key, str) and ("..." in api_key or api_key.endswith("***")):
                api_key = None
            cfg = cfgmod.set_llm(
                provider=provider,
                api_key=api_key,
                model=(body.get("model") or "").strip() or None,
                base_url=(body.get("base_url") or "").strip() or None,
                enabled=bool(body.get("enabled", True)),
                quality=(body.get("quality") or "").strip() or None,
            )
            # transcribe / network / output 这类附带设置
            extra = {}
            for section in ("transcribe", "network", "output", "extract"):
                if isinstance(body.get(section), dict):
                    extra[section] = body[section]
            if extra:
                cfg = cfgmod.save_config(extra)
            _log("配置已更新 provider=%s model=%s" % (
                (cfg.get("llm") or {}).get("provider"),
                (cfg.get("llm") or {}).get("model")))
            self._json(200, {"ok": True, "config": cfgmod.public_config()})
            return
        if path == "/api/config/reset":
            cfgmod.reset_config()
            self._json(200, {"ok": True, "config": cfgmod.public_config()})
            return
        if path == "/api/notes/forget":
            # 从笔记列表移除记录（可选同时删除文件）：POST {"id":..., "delete_files":bool}
            body = self._read_body()
            if body is None:
                return
            tid = (body.get("id") or "").strip()
            if not tid or ".." in tid or "/" in tid or "\\" in tid:
                self._json(400, {"error": "无效任务 ID"})
                return
            with _lock:
                existed = _tasks.pop(tid, None) is not None
            deleted = False
            if body.get("delete_files"):
                tdir = os.path.join(TASKS_DIR, tid)
                base = os.path.realpath(TASKS_DIR)
                real = os.path.realpath(tdir)
                if real.startswith(base + os.sep) and os.path.isdir(real):
                    shutil.rmtree(real, ignore_errors=True)
                    deleted = True
            self._json(200, {"ok": True, "existed": existed, "deleted": deleted})
            return
        if path == "/api/tasks/cleanup":
            # 手动清理任务数据：POST {"force": true/false}
            body = self._read_body()
            if body is None:
                return
            try:
                result = cleanup_tasks(force=bool((body or {}).get("force")))
            except Exception as e:
                self._json(500, {"error": "清理失败：%s" % str(e)[:150]})
                return
            self._json(200, {"ok": True, **result})
            return
        if path == "/api/llm/test":
            # 连通性测试：可以先用 body 里的临时参数，也可以直接测已保存的配置
            body = self._read_body()
            if body is None:
                return
            temp = {}
            if body.get("base_url") and body.get("model"):
                temp = {
                    "provider": (body.get("provider") or "custom").strip(),
                    "base_url": body["base_url"].strip(),
                    "model": body["model"].strip(),
                    "api_key": (body.get("api_key") or "").strip(),
                    "enabled": True,
                    "quality": "standard",
                    "timeout": 30,
                    "max_retries": 0,
                    "is_local": "127.0.0.1" in body["base_url"] or "localhost" in body["base_url"],
                }
            if temp:
                # 临时配置：monkeypatch provider_of，不落盘
                import llm as _llm
                _orig = _llm.provider_of
                _llm.provider_of = lambda cfg=None: temp
                try:
                    ok, msg = test_connection()
                finally:
                    _llm.provider_of = _orig
            else:
                ok, msg = test_connection()
            self._json(200 if ok else 400, {"ok": ok, "message": msg})
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
            self._json(200, _public_task(task))
            return
        if path.startswith("/api/files/"):
            parts = path.split("/")
            if len(parts) == 5:
                tid, fname = parts[3], urllib.parse.unquote(parts[4])
                fpath = _safe_path(tid, fname)
                if fpath and os.path.exists(fpath):
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
                    try:
                        with open(fpath, "rb") as f:
                            self.end_headers()
                            self.wfile.write(f.read())
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                        # 用户取消下载/页面刷新，忽略，不能让服务崩溃
                        pass
                    return
            self._json(404, {"error": "文件不存在"})
            return
        if path == "/api/config":
            self._json(200, cfgmod.public_config())
            return
        if path == "/api/llm/status":
            self._json(200, provider_info())
            return
        if path == "/api/models":
            pid = urllib.parse.parse_qs(parsed.query).get("provider", [""])[0]
            self._json(200, {"provider": pid, "models": cfgmod.models_for(pid)})
            return
        if path == "/api/notes":
            result = []
            seen = set()
            with _lock:
                snapshot = list(_tasks.items())
            for tid, task in snapshot:
                if task.get("done") and task.get("result"):
                    result.append({
                        "id": tid, "title": task["result"].get("title", "笔记"),
                        "type": task.get("type", ""), "typeKey": task.get("typeKey", ""),
                        "src": task.get("url", ""),
                        "fmts": task.get("fmts", []), "time": task.get("ts", 0),
                        "files": task["result"].get("files", {}),
                        "points": task["result"].get("points", 0),
                        "chapters": task["result"].get("chapters", 0),
                        "llm": bool(task["result"].get("llm_enhanced"))
                    })
                    seen.add(tid)
            # 服务重启后内存里的任务没了，但磁盘上有笔记，这里补上历史记录，
            # 否则用户重启一次服务，之前的笔记就在列表里消失（旧版问题）。
            for meta in _load_task_index():
                if meta.get("id") not in seen:
                    result.append(meta)
                    seen.add(meta.get("id"))
            result.sort(key=lambda x: x.get("time", 0), reverse=True)
            self._json(200, {"notes": result})
            return
        if path == "/api/read":
            tid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            fname = urllib.parse.parse_qs(parsed.query).get("file", ["notes.md"])[0]
            fpath = _safe_path(tid, fname)
            if fpath and os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    self._json(200, {"content": f.read()})
                return
            self._json(404, {"error": "文件不存在"})
            return
        self._json(404, {"error": "未知接口"})

    def _json(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            # 客户端中断（如页面刷新/取消下载），忽略即可，绝不能让服务崩溃
            pass


def main():
    ap = argparse.ArgumentParser(description="智学笔记本地后端服务")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print("智学笔记后端服务已启动: http://%s:%d" % (args.host, args.port))
    print("API:")
    print("  POST /api/process    启动整理任务")
    print("  GET  /api/status?id= 查询任务状态")
    print("  GET  /api/files/{id}/{filename}  下载结果文件")
    print("  GET  /api/notes      列出已完成笔记")
    print("  GET  /api/read?id=&file=  读取文件内容")
    print("  POST /api/export/anki       按需生成 Anki 卡片")
    print("  POST /api/task/confirm      重操作前的用户确认（如无字幕视频转写）")
    print("  POST /api/export/obsidian   按需写入 Obsidian Vault")
    print("  GET  /api/config            读取配置（Key 打码）")
    print("  POST /api/config/save       保存配置（大模型/服务商/模型）")
    print("  POST /api/llm/test          大模型连通性测试")
    print("  GET  /api/models?provider=  某服务商的推荐模型列表")
    print("  POST /api/tasks/cleanup     清理过期任务数据")
    print("  POST /api/notes/forget      从笔记列表移除记录")
    print("脚本目录: %s" % SCRIPTS)
    print("任务目录: %s" % TASKS_DIR)
    # 后台定期清理任务数据（避免任务目录与内存字典无限增长）
    threading.Thread(target=_cleanup_loop, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()