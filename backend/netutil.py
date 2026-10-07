#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记 · 通用网络与链接解析

为什么需要这个模块（旧版「部分链接失败」的根因）：
  1. B 站短链 https://b23.tv/xxxx 旧版直接拿正则找 BV 号 —— 短链里根本没有 BV，
     必须跟随 302 跳转才能拿到。这里是首因。
  2. 多 P 视频（合集）旧版只处理第 1 P，用户贴的 ?p=5 被无视。
  3. 手机版分享链接 m.bilibili.com、av 号、带一堆跟踪参数的链接都没处理。
  4. 网络抖动没有任何重试。

对外接口：
    session()                        带默认 UA/Referer 的 Session（requests 缺失时返回 None）
    resolve_bilibili(url)            尽力解析出 (bvid, cid, page_index, title, duration, error)
    bilibili_pages(bvid)             取分 P 列表
    resolve_youtube(url)             规范化 YouTube 链接
    page_title(url)                  抓 <title> / og:title
    http_get(url, ...)               带重试的 GET
    available()                      requests 是否可用
"""
import json
import os
import re
import time
import urllib.parse

try:
    import requests
except ImportError:                       # 允许在没装 requests 时导入本模块
    requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_SESSION = None


def available():
    """requests 是否可用"""
    return requests is not None


def session():
    """全局复用的 Session：统一 UA 与 Referer，避免被 B 站风控挡回"""
    global _SESSION
    if requests is None:
        return None
    if _SESSION is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        _SESSION = s
    return _SESSION


def http_get(url, headers=None, timeout=20, tries=3, allow_redirects=True, **kwargs):
    """
    带指数退避重试的 GET。全部失败时抛最后一个异常。
    旧版各处都是裸 requests.get，一次网络抖动就整单失败。
    """
    s = session()
    if s is None:
        raise RuntimeError("缺少 requests 库，请执行: pip install requests")
    last = None
    for i in range(max(1, tries)):
        try:
            return s.get(url, headers=headers, timeout=timeout,
                         allow_redirects=allow_redirects, **kwargs)
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(1.2 * (i + 1))
    raise last


# ---------------------------------------------------------------------------
# B 站
# ---------------------------------------------------------------------------
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.I)
# 短链域名：这些链接里没有 BV 号，必须跟随跳转
_SHORT_HOSTS = ("b23.tv", "bili2233.cn", "b23.wtf")


def is_bilibili(url):
    return bool(re.search(r"bilibili\.com|b23\.tv|bili2233\.cn", url or "", re.I))


def is_short_link(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return any(h in host for h in _SHORT_HOSTS)


def follow_short_link(url, timeout=15):
    """跟随短链跳转，返回最终 URL（失败返回原 URL）"""
    try:
        r = http_get(url, timeout=timeout, tries=2)
        return r.url or url
    except Exception:
        return url


def _api(url, params=None, timeout=15, tries=3):
    r = http_get(url, params=params, timeout=timeout, tries=tries)
    return r.json()


def av_to_bvid(avid):
    """av 号 -> BV 号（B 站官方算法，av 号在网页里仍常见）"""
    table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
    tr = {c: i for i, c in enumerate(table)}
    s = [11, 10, 3, 8, 4, 6]
    xor = 177451812
    add = 8728348608
    x = (int(avid) ^ xor) + add
    r = list("BV1  4 1 7  ")
    for i in range(6):
        r[s[i]] = table[x // (58 ** i) % 58]
    return "".join(r).replace(" ", "")


def parse_bilibili_input(text):
    """
    从任意文本里抠出 B 站视频标识。
    返回 (bvid 或 None, avid 或 None)；短链返回 (None, None)，需要先跟随跳转。
    """
    m = _BV_RE.search(text or "")
    if m:
        return m.group(1), None
    m = _AV_RE.search(text or "")
    if m:
        return None, m.group(1)
    return None, None


def parse_page_param(url):
    """取 ?p=N（分 P 序号，从 1 开始）；没有返回 1"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        p = int((q.get("p") or ["1"])[0])
        return p if p >= 1 else 1
    except Exception:
        return 1


def resolve_bilibili(url, timeout=15):
    """
    把用户给的各种 B 站链接解析成可用的视频信息。

    返回 dict:
        {"ok": bool, "bvid": str, "cid": int, "page_index": int,
         "title": str, "page_title": str, "duration": int,
         "total_pages": int, "error": str}
    支持：完整链接 / 短链 b23.tv / av 号 / 手机版链接 / 带 ?p=N 的多 P 链接
    """
    out = {"ok": False, "bvid": None, "cid": None, "page_index": 1,
           "title": "", "page_title": "", "duration": 0, "total_pages": 1,
           "error": ""}

    if not is_bilibili(url):
        out["error"] = "不是 B 站链接"
        return out

    worked = url
    if is_short_link(url):
        # 关键修复：短链必须跟随跳转，否则拿不到 BV 号
        worked = follow_short_link(url, timeout=timeout)
        out["resolved_url"] = worked

    bvid, avid = parse_bilibili_input(worked)
    if not bvid and avid:
        try:
            bvid = av_to_bvid(avid)
        except Exception:
            out["error"] = "av 号转换失败：%s" % avid
            return out
    if not bvid:
        out["error"] = ("无法从链接中解析出视频编号（BV 号）。"
                        "请确认链接来自视频播放页，而不是搜索结果页或空间主页。")
        return out

    page_index = parse_page_param(url) or parse_page_param(worked)
    out["bvid"] = bvid
    out["page_index"] = page_index

    try:
        data = _api("https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid}, timeout=timeout)
    except Exception as e:
        out["error"] = "获取视频信息失败：%s" % str(e)[:120]
        return out

    if data.get("code") != 0:
        out["error"] = "B 站接口返回错误 code=%s %s" % (
            data.get("code"), data.get("message"))
        # -404 视频不存在；-403 权限；62002 稿件不可见
        if data.get("code") in (-404, 62002):
            out["error"] = "视频不存在或已失效（可能被删除/设为私密）"
        elif data.get("code") == -403:
            out["error"] = "该视频有访问限制（可能是付费课程或仅粉丝可见）"
        return out

    info = data.get("data") or {}
    pages = info.get("pages") or []
    out["total_pages"] = len(pages) or 1
    out["title"] = info.get("title") or ""

    page = None
    if pages:
        idx = min(max(page_index, 1), len(pages)) - 1
        page = pages[idx]
        out["page_index"] = idx + 1
    if page:
        out["cid"] = page.get("cid")
        out["page_title"] = page.get("part") or out["title"]
        out["duration"] = page.get("duration") or info.get("duration") or 0
    else:
        out["cid"] = info.get("cid")
        out["page_title"] = out["title"]
        out["duration"] = info.get("duration") or 0

    out["ok"] = bool(out["cid"])
    if not out["ok"]:
        out["error"] = "未能获取 cid（视频分 P 信息异常）"
    return out


def bilibili_pages(bvid, timeout=15):
    """取分 P 列表：[{"cid","part","duration","page"}, ...]"""
    try:
        data = _api("https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid}, timeout=timeout)
        if data.get("code") != 0:
            return []
        return (data.get("data") or {}).get("pages") or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
def is_youtube(url):
    return bool(re.search(r"youtube\.com|youtu\.be", url or "", re.I))


def resolve_youtube(url, timeout=15):
    """
    规范化 YouTube 链接，取出视频 ID 与播放列表参数。
    返回 {"ok","video_id","list_id","is_playlist","error"}
    """
    out = {"ok": False, "video_id": None, "list_id": None,
           "is_playlist": False, "error": ""}
    if not is_youtube(url):
        out["error"] = "不是 YouTube 链接"
        return out
    try:
        pr = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(pr.query)
        vid = None
        if "youtu.be" in pr.netloc.lower():
            vid = pr.path.strip("/").split("/")[0] or None
        else:
            vid = (q.get("v") or [None])[0]
            if not vid:
                m = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,})", pr.path)
                if m:
                    vid = m.group(1)
        out["video_id"] = vid
        out["list_id"] = (q.get("list") or [None])[0]
        out["is_playlist"] = bool(out["list_id"]) and not vid
        out["ok"] = bool(vid) or out["is_playlist"]
        if not out["ok"]:
            out["error"] = "无法识别 YouTube 视频 ID（可能是频道页或搜索结果页）"
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


# ---------------------------------------------------------------------------
# 通用：抓页面标题
# ---------------------------------------------------------------------------
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+'
    r'content=["\'](.*?)["\']', re.I | re.S)


def extract_title(html):
    """从 HTML 里取标题：优先 og:title，其次 <title>"""
    if not html:
        return ""
    m = _OG_RE.search(html)
    if m and m.group(1).strip():
        title = m.group(1).strip()
    else:
        m = _TITLE_RE.search(html)
        title = m.group(1).strip() if m else ""
    title = re.sub(r"\s+", " ", title)
    # 去掉站点后缀，如 "xxx_哔哩哔哩_bilibili"
    title = re.sub(r"[_\-|]\s*(哔哩哔哩|bilibili|YouTube|知乎|CSDN|简书|博客园|掘金).*$",
                   "", title, flags=re.I)
    return title.strip()[:150]


def page_title(url, timeout=15):
    """快速抓取网页标题（失败返回空串）"""
    try:
        r = http_get(url, timeout=timeout, tries=2)
        return extract_title(getattr(r, "text", "") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 第三方公共阅读器兜底（JS 渲染页面、抓取被拦时）
# ---------------------------------------------------------------------------
def reader_fetch(url, timeout=45):
    """
    通过 r.jina.ai 公共阅读器取正文（无需 Key）。
    返回 {"ok","title","text","error"}。

    注意：这会把你要整理的网址发给第三方服务。默认开启，可在配置里关掉
    （config.network.reader_fallback = false）。
    """
    out = {"ok": False, "title": "", "text": "", "error": ""}
    target = "https://r.jina.ai/" + url
    try:
        r = http_get(target, timeout=timeout, tries=2,
                     headers={"User-Agent": UA, "Accept": "text/plain"})
        if r.status_code != 200:
            out["error"] = "阅读器返回 HTTP %s" % r.status_code
            return out
        text = (r.text or "").strip()
        if len(text) < 200:
            out["error"] = "阅读器返回内容过短"
            return out
        # r.jina.ai 的返回形如：
        #   Title: xxx\n\nURL Source: xxx\n\nMarkdown Content:\n正文...
        title = ""
        m = re.search(r"^Title:\s*(.+)$", text, re.M)
        if m:
            title = m.group(1).strip()
        if "Markdown Content:" in text:
            text = text.split("Markdown Content:", 1)[1].strip()
        out.update({"ok": True, "title": title, "text": text})
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return out


def split_paragraphs(text, min_len=10):
    """把阅读器返回的 Markdown/纯文本切成段落列表"""
    paras = []
    for block in re.split(r"\n\s*\n", text or ""):
        line = re.sub(r"\s+", " ", block).strip()
        # 去掉纯 Markdown 标记行（图片、链接导航、标题符号）
        line = re.sub(r"^#{1,6}\s*", "", line)
        if line.startswith("![") or line.startswith("["):
            continue
        if len(line) >= min_len:
            paras.append(line)
    return paras


if __name__ == "__main__":
    import sys
    print("requests 可用:", available())
    for u in sys.argv[1:]:
        print("\n输入:", u)
        if is_bilibili(u):
            print("B站解析:", json.dumps(resolve_bilibili(u), ensure_ascii=False, indent=1))
        elif is_youtube(u):
            print("YouTube解析:", json.dumps(resolve_youtube(u), ensure_ascii=False, indent=1))
        else:
            print("网页标题:", page_title(u))
