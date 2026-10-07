#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记 · 文档内容抽取

旧版对 PDF / Word 直链直接报「暂不支持自动抓取」。本模块补上这条通道。

支持格式（按依赖情况自动降级）：
    .pdf   → pypdf（纯 Python，无需额外系统组件）
    .docx  → 标准库 zipfile + xml 解析（零依赖）
    .txt / .md → 直接读取（自动探测编码）
    其他   → 返回明确提示

依赖：
    pip install pypdf        # 仅 PDF 需要；不装则 PDF 走"提示"分支，其他格式照常
"""
import io
import json
import os
import re
import zipfile

try:
    from netutil import http_get, extract_title
except ImportError:                       # 允许单文件调试
    def http_get(*a, **k):
        raise RuntimeError("缺少 netutil 模块")

    def extract_title(html):
        return ""


PDF_EXT = (".pdf",)
DOCX_EXT = (".docx",)
TEXT_EXT = (".txt", ".md", ".markdown", ".text")

# 单篇文档最多取多少字，避免几十页 PDF 把后续流程压死
MAX_CHARS = 60000


def pypdf_available():
    try:
        import pypdf                       # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import PyPDF2                      # noqa: F401
        return True
    except ImportError:
        return False


def ext_of(url):
    """取 URL 的扩展名（小写，去掉查询串）"""
    path = url.split("?")[0].split("#")[0]
    return os.path.splitext(path)[1].lower()


def is_pdf(url):
    return ext_of(url) in PDF_EXT


def is_docx(url):
    return ext_of(url) in DOCX_EXT


def is_text_doc(url):
    return ext_of(url) in TEXT_EXT


def is_document(url):
    return is_pdf(url) or is_docx(url) or is_text_doc(url)


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------
def _read_pdf(data):
    """解析 PDF 字节流，返回 (text, page_count, error)"""
    text, pages = "", 0
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
        for page in reader.pages:
            try:
                text += (page.extract_text() or "") + "\n\n"
            except Exception:
                continue
    except ImportError:
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
            for page in reader.pages:
                try:
                    text += (page.extract_text() or "") + "\n\n"
                except Exception:
                    continue
        except ImportError:
            return "", 0, ("未安装 PDF 解析库。请执行：pip install pypdf"
                           "（装好后重启服务即可自动支持 PDF 链接）")
        except Exception as e:
            return "", 0, "PDF 解析失败：%s" % str(e)[:120]
    except Exception as e:
        return "", 0, "PDF 解析失败：%s" % str(e)[:120]

    text = _clean(text)
    if len(text) < 50:
        return text, pages, ("这份 PDF 提取不到文字，可能是扫描版（图片型 PDF）。"
                             "可先用 OCR 工具转成文字，或把内容复制到输入框。")
    return text, pages, ""


def _read_docx(data):
    """解析 docx（本质是 zip + xml），零依赖"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n == "word/document.xml"]
            if not names:
                return "", "这份 docx 结构异常（缺少 word/document.xml）"
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception as e:
        return "", "docx 解析失败：%s" % str(e)[:120]

    # <w:p> 是段落边界，转成换行；<w:tab> 制表位转空格
    xml = xml.replace("</w:p>", "\n")
    xml = xml.replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    text = re.sub(r"<[^>]+>", "", xml)          # 去标签
    text = _unescape_xml(text)
    text = _clean(text)
    if len(text) < 50:
        return text, "这份 docx 提取不到文字（可能是空文档或纯图片）"
    return text, ""


def _read_text(data):
    """文本文件：多编码试探"""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return _clean(data.decode(enc))
        except (UnicodeDecodeError, LookupError):
            continue
    return _clean(data.decode("utf-8", "ignore"))


def _unescape_xml(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def _clean(text):
    """统一空白、去 BOM、去掉多余空行"""
    # BOM 必须去掉：UTF-8 BOM 解出来是 \ufeff，留在正文开头会污染标题与首句
    text = text.lstrip("\ufeff")
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_to_paragraphs(text, min_len=15):
    """把文档正文切成段落列表（给后续提炼用）"""
    paras = []
    for block in re.split(r"\n\s*\n", text or ""):
        for line in block.split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if len(line) >= min_len:
                paras.append(line)
    return paras


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def fetch_document(url, timeout=60):
    """
    下载并解析文档直链。

    返回:
        {"ok": bool, "title": str, "paragraphs": [str], "text": str,
         "pages": int, "kind": "pdf|docx|text", "error": str}
    """
    out = {"ok": False, "title": "", "paragraphs": [], "text": "",
           "pages": 0, "kind": "", "error": ""}

    kind = ""
    if is_pdf(url):
        kind = "pdf"
    elif is_docx(url):
        kind = "docx"
    elif is_text_doc(url):
        kind = "text"
    else:
        out["error"] = "不支持的文档类型（目前支持 pdf / docx / txt / md）"
        return out
    out["kind"] = kind

    try:
        r = http_get(url, timeout=timeout, tries=3)
        if r.status_code != 200:
            out["error"] = "下载失败：HTTP %s" % r.status_code
            return out
        data = r.content or b""
    except Exception as e:
        out["error"] = "下载失败：%s: %s" % (type(e).__name__, str(e)[:120])
        return out

    if not data:
        out["error"] = "下载到的文件是空的"
        return out

    if kind == "pdf":
        text, pages, err = _read_pdf(data)
        out["pages"] = pages
        if err:
            out["error"] = err
            return out
    elif kind == "docx":
        text, err = _read_docx(data)
        if err:
            out["error"] = err
            return out
    else:
        text = _read_text(data)

    # 标题：优先用文件名，其次 PDF 元数据里的标题
    name = os.path.basename(url.split("?")[0])
    title = os.path.splitext(name)[0]
    title = re.sub(r"[_\-]+", " ", title).strip()

    out["text"] = text[:MAX_CHARS]
    out["paragraphs"] = text_to_paragraphs(out["text"])
    out["title"] = title[:100]
    out["ok"] = len(out["paragraphs"]) >= 1
    if not out["ok"]:
        out["error"] = out["error"] or "文档内容过少，无法整理"
    return out


def download_to(url, dest_path, timeout=120):
    """把文档下载到本地（供上传/缓存用）"""
    try:
        r = http_get(url, timeout=timeout, tries=3)
        if r.status_code != 200:
            return False, "HTTP %s" % r.status_code
        with open(dest_path, "wb") as f:
            f.write(r.content or b"")
        return True, ""
    except Exception as e:
        return False, str(e)[:120]


if __name__ == "__main__":
    import sys
    print("pypdf 可用:", pypdf_available())
    for u in sys.argv[1:]:
        print("\n", u)
        print(json.dumps({k: (v if k != "text" else v[:200] + "...")
                          for k, v in fetch_document(u).items()},
                         ensure_ascii=False, indent=1))
