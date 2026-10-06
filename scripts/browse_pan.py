#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网盘分享文件夹浏览器 — 列出分享链接中的文件清单

用法:
    python browse_pan.py <分享链接> [--json]

依赖:
    python -m pip install playwright && playwright install chromium

说明:
    打开网盘分享页面（百度网盘/阿里云盘/夸克等），列出文件名与大小。
    需要提取码的页面会输出 need_password 标记，由上层引导用户补充。
"""
import argparse
import json
import re
import sys


def browse(url, timeout_ms=30000, out_path=None):
    from playwright.sync_api import sync_playwright
    result = {"files": [], "need_password": False, "error": None, "title": ""}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            result["title"] = page.title()

            # 检测提取码输入框
            pwd_input = page.query_selector(
                "input[placeholder*='提取码'], input[placeholder*='密码'], "
                "input[type='password']")
            if pwd_input:
                result["need_password"] = True
                browser.close()
                _emit(result, out_path)
                return result

            # 通用文件名提取策略
            selectors = [
                ".file-name", ".filename", ".file-name-text",
                "td[title]", ".file-list li", ".dir-item",
                "[class*='fileItem'] [class*='name']",
                "[class*='file-item']", ".list-controller li",
            ]
            names = set()
            for sel in selectors:
                try:
                    els = page.query_selector_all(sel)
                    for el in els[:100]:
                        t = (el.get_attribute("title") or el.inner_text() or "").strip()
                        if t and 1 < len(t) < 200 and not re.match(r"^\d+(\.\d+)?\s*[KMG]?$", t):
                            names.add(t)
                except Exception:
                    continue
            result["files"] = [{"name": n} for n in sorted(names)[:200]]
        except Exception as e:
            result["error"] = str(e)[:200]
        finally:
            try:
                browser.close()
            except Exception:
                pass
    _emit(result, out_path)
    return result


def _emit(result, out_path):
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="网盘分享文件浏览")
    ap.add_argument("url", help="网盘分享链接")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", default=None, help="结果写入指定 JSON 文件（供后端调用）")
    args = ap.parse_args()
    try:
        result = browse(args.url, out_path=args.out)
    except ImportError:
        result = {"files": [], "need_password": False,
                  "error": "未安装 playwright，请执行: pip install playwright && playwright install chromium",
                  "title": ""}
        if args.out:
            _emit(result, args.out)
    if args.out:
        print("结果已写入: %s" % args.out)
        return
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["error"]:
            print("错误: %s" % result["error"])
            sys.exit(1)
        if result["need_password"]:
            print("该分享需要提取码")
            sys.exit(2)
        if not result["files"]:
            print("未识别到文件（页面结构可能变化或需要登录）")
            sys.exit(3)
        print("页面: %s" % result["title"])
        for f in result["files"]:
            print("  - %s" % f["name"])


if __name__ == "__main__":
    main()