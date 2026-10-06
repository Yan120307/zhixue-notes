#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频关键帧自动截图脚本（Playwright 方案，备选）
在网页视频播放器中定位到指定时间点并截图，用于"自动截屏重点内容"。

用法:
    python screenshot_keyframes.py <视频URL> --points 120,345,678 --out-dir screenshots
    python screenshot_keyframes.py <视频URL> --points "12:30,23:45" --out-dir screenshots
    python screenshot_keyframes.py <视频URL> --points 90 --out-dir screenshots --wait 3

参数:
    --points     逗号分隔的时间点，支持秒数或 mm:ss / hh:mm:ss
    --out-dir    截图输出目录（默认 .temp/screenshots）
    --wait       跳转后等待秒数（默认 3 秒，让画面渲染完成）
    --selector   video 元素选择器（默认自动探测：video、.bpx-player-video-wrap video 等）

依赖:
    pip install playwright && playwright install chromium

说明:
  - 本脚本为可选的高效批量截图方案；更稳的方式是用内置 Playwright 浏览器工具逐帧截图
    （见 references/screenshot-guide.md）。两者输出同样为 PNG 文件。
  - 时间点一般来自字幕时间轴（重点知识对应的起始时间）。
"""
import argparse
import os
import re
import sys
import time


def parse_time(t: str) -> int:
    t = t.strip()
    parts = t.split(":")
    if len(parts) == 3:
        h, m, s = (int(x) for x in parts)
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = (int(x) for x in parts)
        return m * 60 + s
    return int(float(t))


def main():
    ap = argparse.ArgumentParser(description="视频关键帧截图")
    ap.add_argument("url", help="视频页面 URL")
    ap.add_argument("--points", required=True, help="逗号分隔的时间点")
    ap.add_argument("--out-dir", default=".temp/screenshots", help="输出目录")
    ap.add_argument("--wait", type=float, default=3.0, help="跳转后等待秒数")
    ap.add_argument("--selector", default=None, help="video 元素选择器")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: 缺少 playwright, 请先执行: pip install playwright && playwright install chromium")
        sys.exit(1)

    points = [parse_time(p) for p in args.points.split(",") if p.strip()]
    os.makedirs(args.out_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        print(f"打开页面: {args.url}")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        selector = args.selector
        if not selector:
            for sel in ["video", ".bpx-player-video-wrap video", "video[src]", "#player video"]:
                try:
                    if page.locator(sel).count() > 0:
                        selector = sel
                        break
                except Exception:
                    continue
        if not selector:
            print("ERROR: 未找到 video 元素, 请通过 --selector 指定")
            browser.close()
            sys.exit(1)
        print(f"使用选择器: {selector}")

        # 尝试让视频进入可播放状态
        try:
            page.locator(selector).first.click()
        except Exception:
            pass

        for i, sec in enumerate(points):
            label = f"{i + 1:02d}"
            try:
                # 直接设置 currentTime 定位
                page.evaluate(
                    """(args) => {
                        const v = document.querySelector(args.sel);
                        if (!v) return;
                        v.currentTime = args.sec;
                        v.play && v.play().catch(() => {});
                    }""",
                    {"sel": selector, "sec": sec},
                )
                page.wait_for_timeout(int(args.wait * 1000))
                # 再确认一次定位（部分播放器会重载视频）
                page.evaluate(
                    """(args) => {
                        const v = document.querySelector(args.sel);
                        if (v && Math.abs(v.currentTime - args.sec) > 2) v.currentTime = args.sec;
                    }""",
                    {"sel": selector, "sec": sec},
                )
                page.wait_for_timeout(800)
                out = os.path.join(args.out_dir, f"keyframe_{label(sec)}_{i + 1:02d}.png")
                page.locator(selector).first.screenshot(path=out)
                print(f"截图 {i + 1}/{len(points)}: {out} (t={sec}s)")
            except Exception as e:
                print(f"截图 {i + 1}/{len(points)} 失败 (t={sec}s): {e}")
        browser.close()


def label(sec: int) -> str:
    return f"{sec // 3600:02d}-{sec % 3600 // 60:02d}-{sec % 60:02d}"


if __name__ == "__main__":
    main()