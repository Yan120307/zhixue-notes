#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网课字幕/文稿抓取脚本（多平台）
用法:
    python fetch_subtitles.py <视频URL> [--out-dir 输出目录] [--fmt txt|srt|json] [--cid CID]
示例:
    python fetch_subtitles.py "https://www.bilibili.com/video/BV1xx411c7mD"
    python fetch_subtitles.py "https://www.youtube.com/watch?v=xxxx"
依赖:
  - requests (如缺失: pip install requests)
  - B站: 复用成熟 WBI 签名 + 字幕列表接口方案
  - YouTube: 调用 yt-dlp (需安装: pip install yt-dlp 或 scoop/choco 安装)
说明:
  - 退出码 0 = 成功，字幕已保存
  - 退出码 2 = 无字幕或需登录，上层流程应转用 Playwright 方案
  - 退出码 3 = 未知平台，同样转用 Playwright 通用方案
  - 多 P 视频默认抓取第一 P, 可传 --cid 指定分 P
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:
    print("ERROR: 缺少 requests 库, 请先执行: pip install requests")
    sys.exit(2)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})

# WBI 签名重排表 (公开常量)
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def get_wbi_keys() -> str:
    """从 nav 接口获取 wbi_img key 并计算 mixin key"""
    resp = SESSION.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
    data = resp.json()
    img_url = data["data"]["wbi_img"]["img_url"]
    sub_url = data["data"]["wbi_img"]["sub_url"]
    img_key = os.path.splitext(os.path.basename(img_url))[0]
    sub_key = os.path.splitext(os.path.basename(sub_url))[0]
    raw = img_key + sub_key
    mixin = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    return mixin


def enc_wbi(params: dict, mixin_key: str) -> dict:
    """对参数加 wts 时间戳、排序、MD5 签名, 返回带签名的完整参数"""
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params


def parse_bvid(text: str) -> str:
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if m:
        return m.group(1)
    raise ValueError("无法从输入中解析出 BV 号, 请提供完整视频 URL 或 BV 号")


def get_video_info(bvid: str) -> dict:
    url = "https://api.bilibili.com/x/web-interface/view"
    resp = SESSION.get(url, params={"bvid": bvid}, timeout=15)
    data = resp.json()
    if data["code"] != 0:
        raise RuntimeError(f"获取视频信息失败: code={data['code']} msg={data.get('message')}")
    return data["data"]


def get_subtitle_list(bvid: str, cid: int, mixin_key: str) -> list:
    """调用播放器接口获取字幕列表, 返回 subtitles 数组"""
    params = enc_wbi({"bvid": bvid, "cid": cid}, mixin_key)
    url = "https://api.bilibili.com/x/player/wbi/v2"
    resp = SESSION.get(url, params=params, timeout=10)
    data = resp.json()
    if data["code"] != 0:
        # 回退到无签名接口再试一次
        resp = SESSION.get(
            "https://api.bilibili.com/x/player/v2",
            params={"bvid": bvid, "cid": cid}, timeout=10)
        data = resp.json()
        if data["code"] != 0:
            raise RuntimeError(f"获取字幕列表失败: code={data['code']} {data.get('message')}")
    sub_data = (data.get("data") or {}).get("subtitle") or {}
    return sub_data.get("subtitles") or []


def download_subtitle(sub_url: str) -> list:
    """下载单个字幕 JSON, 返回 [{from, to, content}, ...]"""
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    resp = SESSION.get(sub_url, timeout=10)
    data = resp.json()
    return data.get("body") or []


def srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(body: list) -> str:
    lines = []
    for i, item in enumerate(body, 1):
        start = srt_timestamp(item.get("from", 0))
        end = srt_timestamp(item.get("to", 0))
        content = item.get("content", "").replace("\n", " ")
        lines.append(f"{i}\n{start} --> {end}\n{content}\n")
    return "\n".join(lines)


def to_txt(body: list) -> str:
    return "\n".join(item.get("content", "").replace("\n", " ") for item in body)


def subtitle_score(s: dict) -> int:
    """字幕优先级: 中文(官方/CC) > 中文(AI) > 其他语言"""
    lang = s.get("lan", "")
    is_ai = s.get("ai_status", -1)
    if "zh" in lang or "cn" in lang.lower():
        return 100 if is_ai == -1 else 60
    return 10


def write_meta(args, meta: dict):
    """把视频元信息写入 --meta-out 指定的 JSON 文件"""
    if not getattr(args, "meta_out", None):
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(args.meta_out)), exist_ok=True)
        with open(args.meta_out, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"警告: 写入元信息失败: {e}")


def fetch_bilibili(args) -> int:
    try:
        bvid = parse_bvid(args.input)
    except ValueError as e:
        print(f"错误: {e}")
        return 1

    print(f"[1/4] 获取视频信息: {bvid}")
    info = get_video_info(bvid)
    title = re.sub(r'[\\/:*?"<>|]', "_", info["title"])
    pages = info["pages"]
    if args.cid:
        pages = [p for p in pages if p["cid"] == args.cid]
        if not pages:
            print(f"错误: 未找到 cid={args.cid} 的分P")
            return 1
    page = pages[0]
    cid = page["cid"]
    part = page.get("part") or title
    print(f"[2/4] 视频: {title} | 分P: {part} | cid={cid}")

    # 无论后续字幕是否抓取成功，先落盘视频元信息（供后端使用真实标题）
    write_meta(args, {
        "platform": "bilibili",
        "bvid": bvid,
        "cid": cid,
        "title": info.get("title", ""),
        "part": part,
        "duration": info.get("duration", 0),
    })

    print("[3/4] 计算 WBI 签名并获取字幕列表 ...")
    mixin_key = get_wbi_keys()
    subtitles = get_subtitle_list(bvid, cid, mixin_key)
    if not subtitles:
        print(
            "警告: 该视频无 CC 字幕或需要登录才能查看。\n"
            "      请转用 Playwright 方案: 检查 __playinfo__ / 提取音频流 + faster-whisper 转写。"
        )
        return 2

    subtitles.sort(key=subtitle_score, reverse=True)
    chosen = subtitles[0]
    lan_doc = chosen.get("lan_doc") or "中文"
    print(f"[4/4] 选用字幕: {chosen.get('lan')} ({lan_doc})")

    body = download_subtitle(chosen["subtitle_url"])
    if not body:
        print("错误: 字幕文件解析为空")
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    safe_part = re.sub(r'[\\/:*?"<>|]', "_", part)
    base = os.path.join(args.out_dir, f"{title}_{safe_part}")
    if args.fmt == "srt":
        out_path = base + ".srt"
        content = to_srt(body)
    elif args.fmt == "json":
        out_path = base + ".json"
        content = json.dumps(body, ensure_ascii=False, indent=2)
    else:
        out_path = base + ".txt"
        content = to_txt(body)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"已保存: {out_path} ({len(body)} 条字幕, {len(content)} 字符)")
    return 0


def fetch_youtube(args) -> int:
    """调用 yt-dlp 抓取 YouTube 字幕"""
    try:
        result = subprocess.run(
            ["yt-dlp", "--write-auto-subs", "--write-subs", "--sub-lang", "zh,en",
             "--skip-download", "--sub-format", "json3/srt/vtt", "-o",
             os.path.join(args.out_dir, "%(id)s.%(ext)s"), args.input],
            capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        print("ERROR: 未安装 yt-dlp, 请先执行: pip install yt-dlp")
        return 2
    print(result.stdout[-3000:] if result.stdout else "")
    print(result.stderr[-2000:] if result.stderr else "")
    if result.returncode != 0:
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description="网课字幕抓取")
    ap.add_argument("input", help="视频URL")
    ap.add_argument("--out-dir", default=".", help="输出目录")
    ap.add_argument("--fmt", choices=["txt", "srt", "json"], default="txt")
    ap.add_argument("--cid", type=int, default=None, help="指定分P的cid")
    ap.add_argument("--meta-out", default=None, help="视频元信息写入指定 JSON 文件（供后端读取真实标题）")
    args = ap.parse_args()

    url_lower = args.input.lower()
    if "bilibili.com" in url_lower or "b23.tv" in url_lower:
        code = fetch_bilibili(args)
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        code = fetch_youtube(args)
    else:
        print("未知平台或非通用平台, 请使用 Playwright 通用方案抓取字幕/文稿。")
        code = 3
    sys.exit(code)


if __name__ == "__main__":
    main()