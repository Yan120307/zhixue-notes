#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无字幕视频转写 — 下载 B 站音频流并用 faster-whisper 本地转写生成字幕

用法:
    python transcribe_audio.py <B站视频URL> --task-dir <任务目录> [--max-seconds 14400]

流程:
    1. 解析 BV 号 → playurl 接口获取 DASH 音频流（最低码率即可）
    2. 流式下载音频到任务目录 audio.m4a（已存在则跳过）
    3. faster-whisper (small / int8 / beam_size=1 / VAD) 转写
    4. 输出 transcript.srt + 更新进度文件 transcode_progress.json

进度文件格式:
    {"stage": "download|transcribe|done|error", "percent": 0-100, "message": "..."}

依赖:
    pip install faster-whisper requests
    (faster-whisper 内置 PyAV 解码，无需安装 ffmpeg 可执行文件)

退出码: 0 = 成功（transcript.srt 已生成），2 = 失败（原因见进度文件）
"""
import argparse
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 缺少 requests 库")
    sys.exit(2)

# 复用 fetch_subtitles 的 WBI 签名与请求会话
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_subtitles import SESSION, get_wbi_keys, enc_wbi, parse_bvid, get_video_info

PROGRESS_FILE = "transcode_progress.json"
AUDIO_FILE = "audio.m4a"
SRT_FILE = "transcript.srt"

# 高频术语校正表：small 模型对中文教学术语的常见误转（只收录把握大的）
TERM_FIXES = {
    "净质": "进制",
    "进质": "进制",
    "提形": "题型",
    "涵数": "函数",
    "字方": "次方",
    "三净形": "三角形",
    "导苏": "导数",
    "微基分": "微积分",
    "矩陈": "矩阵",
    "特征值分解": "特征值分解",
    "概绿": "概率",
    "期望值方差": "期望方差",
}


def fix_terms(text):
    for wrong, right in TERM_FIXES.items():
        if wrong != right:
            text = text.replace(wrong, right)
    return text


def write_progress(task_dir, stage, percent, message=""):
    p = os.path.join(task_dir, PROGRESS_FILE)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "percent": round(percent, 1),
                       "message": message, "ts": time.time()}, f, ensure_ascii=False)
    except Exception:
        pass


def _retry_get(urls, headers, timeout=90, tries=3, desc="请求", **kwargs):
    """带重试的多 URL 请求：主 URL 失败自动换备选 CDN，指数退避"""
    if isinstance(urls, str):
        urls = [urls]
    last_err = None
    for url in urls:
        for i in range(tries):
            try:
                return SESSION.get(url, headers=headers, timeout=timeout, **kwargs)
            except Exception as e:
                last_err = e
                if i < tries - 1:
                    time.sleep(2 * (i + 1))
    raise RuntimeError("%s网络连接失败（已重试 %d 次 × %d 个地址）: %s" % (
        desc, tries, len(urls), str(last_err)[:90]))


def _probe_total(url, headers):
    """用 Range 探测文件真实总大小（B站对匿名完整 GET 限长，但 Range 可取任意区间）"""
    try:
        r = _retry_get(url, dict(headers, Range="bytes=0-0"), timeout=30, tries=2, desc="探测文件大小")
        cr = r.headers.get("Content-Range", "")
        m = re.search(r"/(\d+)$", cr)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0


def download_audio(bvid, task_dir, progress):
    """
    分块 Range 下载最低码率音频流（B站匿名完整 GET 会被截断，必须分块）。

    已知限制：本函数只走 B 站 playurl 接口，因此**仅支持 B 站视频**。
    YouTube 无字幕时不在本脚本处理范围内，需要 yt-dlp 下载音频后才能转写。
    """
    audio_path = os.path.join(task_dir, AUDIO_FILE)
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 100_000:
        write_progress(task_dir, "transcribe", 0, "音频已就绪（上次下载），开始转写")
        return audio_path

    info = get_video_info(bvid)
    cid = info["pages"][0]["cid"]
    mixin = get_wbi_keys()
    params = enc_wbi({"bvid": bvid, "cid": cid, "qn": 64, "fnval": 16,
                      "fnver": 0, "fourk": 0, "platform": "pc"}, mixin)
    r = _retry_get("https://api.bilibili.com/x/player/playurl", {}, params=params, timeout=20, tries=3, desc="获取音频流")
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError("获取音频流失败: %s" % data.get("message"))
    audios = ((data.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audios:
        raise RuntimeError("该视频没有可用的音频流")

    # 选最低码率（转写足够，下载最快）；收集主/备 CDN 地址用于重试切换
    stream = min(audios, key=lambda a: int(a.get("bandwidth") or 10**9))
    urls = [stream["baseUrl"]]
    for key in ("backupUrl", "backupUrls"):
        extra = stream.get(key) or []
        if isinstance(extra, str):
            extra = [extra]
        urls.extend(u for u in extra if u and u not in urls)
    headers = {"Referer": "https://www.bilibili.com/", "User-Agent": SESSION.headers.get("User-Agent", "")}

    total = _probe_total(urls, headers)
    if not total:
        raise RuntimeError("无法探测音频文件大小（接口异常），请稍后重试")

    # 断点续传：.part 文件保留已下载块，重跑时从断点继续
    tmp = audio_path + ".part"
    pos = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    CHUNK = 4 * 1024 * 1024
    t0 = time.time()
    if pos < total:
        with open(tmp, "ab") as f:
            while pos < total:
                end = min(pos + CHUNK, total) - 1
                try:
                    rr = _retry_get(urls, dict(headers, Range="bytes=%d-%d" % (pos, end)),
                                    timeout=90, tries=3, desc="下载音频分块")
                except Exception as e:
                    raise RuntimeError("%s 已保留断点，重新整理将从断点续传" % str(e)[:120])
                if rr.status_code not in (200, 206):
                    raise RuntimeError("音频分块下载失败 HTTP %d" % rr.status_code)
                data = rr.content
                if not data:
                    raise RuntimeError("音频分块下载返回空数据")
                f.write(data)
                pos += len(data)
                write_progress(task_dir, "download", pos * 100.0 / total,
                               "下载音频中 %.0f%%（%.1f MB / %.1f MB）" % (
                                   pos * 100.0 / total, pos / 1048576, total / 1048576))
    if pos < total * 0.98:
        raise RuntimeError("音频下载不完整（%d/%d 字节），请重新整理" % (pos, total))
    os.replace(tmp, audio_path)
    dt = time.time() - t0
    write_progress(task_dir, "transcribe", 0,
                   "音频下载完成（%.1f MB，%.0f 秒），开始本地转写…" % (
                       pos / 1048576, dt))
    return audio_path


def srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _load_model():
    """加载转写模型：优先纯离线（模型已缓存时绝不联网检查更新，
    避免网络抖动导致 ConnectError）；无缓存时再联网下载"""
    from faster_whisper import WhisperModel
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        return WhisperModel("small", device="cpu", compute_type="int8")
    except Exception:
        # 本地无缓存，联网下载（首次使用需要网络）
        os.environ["HF_HUB_OFFLINE"] = "0"
        return WhisperModel("small", device="cpu", compute_type="int8")


def transcribe(audio_path, task_dir, progress):
    """faster-whisper 转写，实时写进度文件，输出 srt"""
    write_progress(task_dir, "transcribe", 0, "加载转写模型（small）…")
    model = _load_model()

    # VAD 过滤静音，显著提速
    segments, info = model.transcribe(
        audio_path, beam_size=1, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False)

    total_sec = info.duration or 0
    items = []
    t0 = time.time()
    for seg in segments:
        text = fix_terms((seg.text or "").strip())
        if text:
            items.append((seg.start, text))
        if total_sec:
            pct = min(99.0, seg.end * 100.0 / total_sec)
            elapsed = time.time() - t0
            speed = seg.end / elapsed if elapsed > 1 else 0
            remain = (total_sec - seg.end) / speed if speed > 0.05 else 0
            write_progress(task_dir, "transcribe", pct,
                           "本地转写中 %.0f%%（预计剩余 %s）" % (
                               pct, _fmt_remain(remain)))

    if not items:
        raise RuntimeError("转写结果为空（音频可能无语音内容）")

    out = os.path.join(task_dir, SRT_FILE)
    with open(out, "w", encoding="utf-8") as f:
        for i, (start, text) in enumerate(items, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (
                i, srt_timestamp(start), srt_timestamp(start + 4), text))

    # 转写完成：删除音频媒体文件（用户约定：笔记生成后不留视频/音频）
    try:
        audio = os.path.join(task_dir, AUDIO_FILE)
        if os.path.exists(audio):
            os.remove(audio)
    except Exception:
        pass
    return out, len(items)


def _fmt_remain(sec):
    if sec <= 0:
        return "计算中…"
    if sec < 90:
        return "约 %.0f 秒" % sec
    return "约 %.0f 分钟" % (sec / 60)


def main():
    ap = argparse.ArgumentParser(description="无字幕视频本地转写")
    ap.add_argument("url", help="B站视频 URL 或 BV 号")
    ap.add_argument("--task-dir", required=True, help="任务目录")
    ap.add_argument("--max-seconds", type=int, default=14400, help="整体超时（秒）")
    args = ap.parse_args()

    task_dir = args.task_dir
    os.makedirs(task_dir, exist_ok=True)

    # 已有转写结果则直接成功（断点续转）
    done_srt = os.path.join(task_dir, SRT_FILE)
    if os.path.exists(done_srt):
        write_progress(task_dir, "done", 100, "已有转写结果，直接使用")
        print("OK: transcript.srt 已存在")
        return

    t_start = time.time()
    try:
        bvid = parse_bvid(args.url)
        write_progress(task_dir, "download", 0, "获取音频流…")
        audio = download_audio(bvid, task_dir, None)
        if time.time() - t_start > args.max_seconds:
            raise RuntimeError("超时")
        out, n = transcribe(audio, task_dir, None)
        write_progress(task_dir, "done", 100, "转写完成（%d 条）" % n)
        print("OK: %s (%d segments, %.0f 秒)" % (out, n, time.time() - t_start))
    except Exception as e:
        write_progress(task_dir, "error", 0, str(e)[:200])
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
