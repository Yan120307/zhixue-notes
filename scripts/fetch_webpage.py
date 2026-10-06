#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页正文抓取 — 提取文章标题与正文段落

用法:
    python fetch_webpage.py <URL> --out result.json

输出 JSON:
    {"title": "...", "paragraphs": ["...", ...], "count": N, "error": null}

依赖:
    requests (pip install requests)

说明:
    - 零额外依赖，基于标准库 html.parser 提取正文
    - 自动剔除 script/style/nav/header/footer 等噪声标签
    - 段落少于 3 条视为提取失败（可能是 JS 渲染页面）
    - 退出码 0 = 成功，2 = 失败（失败时也写 JSON，error 字段说明原因）
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser

try:
    import requests
except ImportError:
    print("ERROR: 缺少 requests 库, 请先执行: pip install requests")
    sys.exit(2)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 这些标签内的内容全部跳过
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer",
             "aside", "iframe", "form", "button", "svg", "template"}
# 视为正文块（文本按块切分收集）
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "blockquote",
              "dd", "dt", "td", "pre", "figcaption", "summary"}


class TextExtractor:
    """基于 html.parser 的轻量正文提取器"""

    def __init__(self):
        self.title = ""
        self.paragraphs = []

    def parse(self, html_text: str):
        extractor = _InnerExtractor()
        extractor.feed(html_text)
        extractor.close()
        self.title = extractor.title.strip()
        self.paragraphs = extractor.paragraphs
        return self


class _InnerExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = 0
        self.title = ""
        self.paragraphs = []
        self._in_block = None
        self._buf = []

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if len(text) >= 10 and not _is_noise(text, self._in_block):
            self.paragraphs.append(text)
        self._buf = []
        self._in_block = None

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title += 1
        elif tag in BLOCK_TAGS and self.skip_depth == 0:
            if self._in_block is not None:
                self._flush()
            self._in_block = tag
            self._buf = []

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        elif tag == "title" and self.in_title > 0:
            self.in_title -= 1
        elif tag == self._in_block:
            self._flush()

    def handle_data(self, data):
        if self.skip_depth > 0:
            return
        if self.in_title:
            self.title += data
        elif self._in_block:
            self._buf.append(data)


_NAV_PAT = re.compile(
    r"^(首页|导航|登录|注册|关于我们|联系方式|版权|广告|点击|更多|分享|评论|"
    r"上一篇|下一篇|相关推荐|返回|目录|菜单|订阅|搜索)", re.I)

_EMAIL_PAT = re.compile(r"[\w*＊·]{2,}@\s*[\w.]+|\*{3,}")
_HEX_PAT = re.compile(r"\b[0-9a-f]{10,}\b")


def _is_noise(text: str, tag: str = None) -> bool:
    """过滤导航/版权/邮箱/代码杂项噪声文本"""
    if len(text) <= 10:
        return True
    if _NAV_PAT.match(text) and len(text) < 30:
        return True
    # 无中英文的纯符号行
    if not re.search(r"[\u4e00-\u9fa5a-zA-Z]", text):
        return True
    # 邮箱 / 打码用户名
    if _EMAIL_PAT.search(text):
        return True
    # 长十六进制 hash（版本号/commit 等）
    if _HEX_PAT.search(text):
        return True
    # 短列表项：几乎都是侧栏导航链接
    if tag == "li" and len(text) < 25:
        return True
    # "·" 分隔的链接串（页脚相关推荐等）
    if len(re.findall(r"·", text)) >= 3:
        return True
    # 导航式短链接串（如 "xx 教程 yy 教程"）
    if len(text) < 30 and re.search(r"(教程|指南|大全|下载|文档)\s*$", text):
        return True
    return False


def fetch(url: str, timeout: int = 20) -> dict:
    result = {"title": "", "paragraphs": [], "count": 0, "error": None}
    try:
        resp = requests.get(
            url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            result["error"] = "HTTP %d，页面不可访问" % resp.status_code
            return result
        # 修正编码：requests 对无 charset 的页面默认 ISO-8859-1，中文会乱码
        if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        html_text = resp.text
    except requests.exceptions.Timeout:
        result["error"] = "网页请求超时"
        return result
    except requests.exceptions.ConnectionError:
        result["error"] = "无法连接该网页（链接可能失效）"
        return result
    except Exception as e:
        result["error"] = "抓取失败: %s" % str(e)[:120]
        return result

    ext = TextExtractor().parse(html_text)
    # 标题兜底：h1 或首个长段落
    title = ext.title
    if title and len(title) > 60:
        title = title[:60]
    result["title"] = title or ""
    paras = ext.paragraphs
    # 去重（保持顺序）
    seen = set()
    uniq = []
    for p in paras:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    result["paragraphs"] = uniq
    result["count"] = len(uniq)
    if len(uniq) < 3:
        result["error"] = ("未能提取到足够正文（该页面可能需要浏览器渲染，"
                           "或不是文章类页面）。可直接复制网页文字粘贴到输入框。")
    return result


def main():
    ap = argparse.ArgumentParser(description="网页正文抓取")
    ap.add_argument("url", help="网页文章 URL")
    ap.add_argument("--out", default=None, help="结果写入指定 JSON 文件")
    args = ap.parse_args()

    result = fetch(args.url)
    ok = result["error"] is None

    if args.out:
        import os
        out_dir = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(out_dir, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("结果已写入: %s" % args.out)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if not ok:
        print("错误: %s" % result["error"], file=sys.stderr)
        sys.exit(2)
    print("标题: %s" % result["title"])
    print("段落: %d 条" % result["count"])


if __name__ == "__main__":
    main()
