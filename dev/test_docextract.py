# -*- coding: utf-8 -*-
"""
docextract.py 自测：不联网。
手工构造最小合法 PDF / DOCX 字节流，直接测解析器。
"""
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BACKEND = os.path.join(REPO, "backend")
sys.path.insert(0, BACKEND)

try:
    import requests                      # noqa: F401
except ImportError:
    # 未安装 requests 时用最小替身，保证解析逻辑仍可测试
    import types
    _m = types.ModuleType("requests")
    _m.Session = lambda: types.SimpleNamespace(headers={}, get=lambda *a, **k: None)
    _m.exceptions = types.SimpleNamespace(Timeout=Exception, ConnectionError=Exception)
    sys.modules["requests"] = _m

import docextract as D

fails = []
total = [0]


def check(name, cond, extra=""):
    total[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
print("1) 扩展名判定")
check(".pdf", D.is_pdf("https://a.com/x.pdf"))
check(".PDF 大写也要认", D.is_pdf("https://a.com/x.PDF"))
check("带查询串", D.is_pdf("https://a.com/x.pdf?token=1"))
check(".docx", D.is_docx("https://a.com/y.docx"))
check(".md", D.is_text_doc("https://a.com/z.md"))
check("html 不是文档", not D.is_document("https://a.com/page.html"))
check("is_document 汇总", D.is_document("https://a.com/x.pdf") and
      D.is_document("https://a.com/y.docx"))

# ---------------------------------------------------------------------------
print("\n2) 手工构造最小 PDF 并解析")


def make_pdf(text="Hello ZhiXue Notes PDF Content Here"):
    content = "BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content.encode()),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, xref_pos))
    return out.getvalue()


pdf_bytes = make_pdf()
check("构造的 PDF 以 %PDF 开头", pdf_bytes.startswith(b"%PDF"))

text, pages, err = D._read_pdf(pdf_bytes)
if D.pypdf_available():
    check("PDF 解析出页数", pages == 1, pages)
    check("PDF 解析出文字", "ZhiXue" in text or "Hello" in text, (text[:80], err))
    check("解析无错误", err == "", err)
else:
    check("未装 pypdf 时给出安装提示（而不是崩溃）",
          "pypdf" in err and pages == 0, (err, pages))
    print("     （本机未安装 pypdf，已跳过真实解析断言）")

# ---------------------------------------------------------------------------
print("\n3) 手工构造 DOCX 并解析（零依赖，必须始终可用）")


def make_docx(paragraphs):
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs)
    doc_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body>%s</w:body></w:document>' % body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        z.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


docx_bytes = make_docx([
    "第一段：智学笔记支持 Word 文档解析。",
    "第二段：这里验证零依赖的 zip + xml 抽取是否正常工作。",
    "第三段：包含 &amp; 转义字符与 &lt;标签&gt; 测试。",
])
text2, err2 = D._read_docx(docx_bytes)
check("docx 解析无错误", err2 == "", err2)
check("docx 三段都在", text2.count("第") >= 3, text2[:120])
check("XML 转义被还原", "&" in text2 and "<标签>" in text2, text2[:200])
check("不含 XML 标签", "<w:" not in text2 and "w:t" not in text2)

bad_docx = io.BytesIO()
with zipfile.ZipFile(bad_docx, "w") as z:
    z.writestr("other.xml", "x")
t3, e3 = D._read_docx(bad_docx.getvalue())
check("缺 document.xml 时给明确错误", t3 == "" and "结构异常" in e3, e3)

# ---------------------------------------------------------------------------
print("\n4) 文本编码试探")
check("utf-8", D._read_text("中文内容测试".encode("utf-8")) == "中文内容测试")
check("utf-8 BOM", D._read_text("中文内容测试".encode("utf-8-sig")) == "中文内容测试")
check("gb18030", D._read_text("中文内容测试".encode("gb18030")) == "中文内容测试")
check("空字节流不崩", D._read_text(b"") == "")

# ---------------------------------------------------------------------------
print("\n5) 段落切分与清洗")
long_text = ("标题行\n\n" + "这是第一段足够长的正文内容，用于测试切分。" * 2 +
             "\n\n短\n\n" + "这是第二段足够长的正文内容，同样用于测试。" * 2)
paras = D.text_to_paragraphs(long_text, min_len=15)
check("切出段落", len(paras) == 2, paras)
check("过短的段落被丢弃", all(len(p) >= 15 for p in paras))
check("多余空白被压掉", "  " not in D._clean("a   b\t\tc"))

# ---------------------------------------------------------------------------
print("\n6) fetch_document 的失败路径（不联网，只测分支）")
r = D.fetch_document("https://a.com/file.zip")
check("不支持的类型给明确提示", r["ok"] is False and "不支持" in r["error"], r)
r = D.fetch_document("https://a.com/nothing.xyz")
check("未知类型不抛异常", isinstance(r, dict) and r["ok"] is False)

print("\n" + "=" * 50)
print("结果：%d 项检查，%d 项通过，%d 项失败" %
      (total[0], total[0] - len(fails), len(fails)))
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("docextract.py 自测通过")
