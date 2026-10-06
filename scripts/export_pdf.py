#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_pdf.py — Markdown 网课笔记 → PDF（中文友好、内嵌截图、表格与链接）

用法:
    python export_pdf.py <笔记.md> [-o 输出.pdf] [--title 标题] [--img-base 图片目录]

依赖:
    python -m pip install reportlab
    中文字体自动探测（微软雅黑 → 黑体 → 宋体 → 内置 CID 字体），Windows 开箱即用

特性:
    - 标题层级 / 无序有序列表 / 复选框 / 引用块 / 分隔线 / 代码块
    - 图片内嵌（相对路径按 md 所在目录或 --img-base 解析，自动缩放至页宽）
    - 行内粗体、斜体、行内代码、可点击超链接
    - 表格（按内容自动列宽、表头着色）
    - 页脚页码；自动清洗 CJK 字体无法显示的 emoji
"""
import argparse
import os
import re
import sys

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (HRFlowable, Image, KeepTogether, Paragraph,
                                Preformatted, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

ACCENT = HexColor('#0D9488')
DARK = HexColor('#0F172A')
GRAY = HexColor('#64748B')
LINEC = HexColor('#CBD5E1')

EMOJI_RANGES = [
    (0x1F000, 0x1FAFF), (0x2600, 0x26FF), (0x2700, 0x27BF),
    (0xFE00, 0xFE0F), (0x1F1E6, 0x1F1FF), (0x200D, 0x200D),
]

_STATE = {'normal': 'STSong-Light', 'bold': 'STSong-Light', 'family': 'CJKFAM'}


def sanitize(text: str) -> str:
    rep = {'☑': '√', '✅': '√', '✓': '√', '✗': '×', '✘': '×', '☐': '□'}
    for k, v in rep.items():
        text = text.replace(k, v)
    out = []
    for ch in text:
        cp = ord(ch)
        if any(a <= cp <= b for a, b in EMOJI_RANGES):
            continue
        out.append(ch)
    return re.sub(r' {2,}', ' ', ''.join(out))


def esc(t: str) -> str:
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def inline_md(md: str) -> str:
    """Markdown 行内语法 → reportlab 段内标记"""
    t = esc(sanitize(md))
    t = re.sub(
        r'\[([^\]]+)\]\(([^)\s]+)\)',
        lambda m: '<link href="%s" color="#2563EB"><u>%s</u></link>'
        % (m.group(2).replace('&', '&amp;'), m.group(1)), t)
    t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', t)
    t = re.sub(r'`([^`]+)`', r'<font face="Courier" color="#B3393A">\1</font>', t)
    return t


def register_fonts():
    normal_candidates = [
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        'C:/Windows/Fonts/simsun.ttc',
        'C:/Windows/Fonts/simfang.ttf',
    ]
    bold_candidates = [
        'C:/Windows/Fonts/msyhbd.ttc',
        'C:/Windows/Fonts/simhei.ttf',
    ]
    ok = False
    for path in normal_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont('CJKREG', path, subfontIndex=0))
                _STATE['normal'] = 'CJKREG'
                ok = True
                break
            except Exception:
                continue
    if not ok:
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        _STATE['normal'] = 'STSong-Light'
    bold_ok = False
    if _STATE['normal'] == 'CJKREG':
        for path in bold_candidates:
            if os.path.exists(path):
                try:
                    pdfmetrics.registerFont(TTFont('CJKBOLD', path, subfontIndex=0))
                    _STATE['bold'] = 'CJKBOLD'
                    bold_ok = True
                    break
                except Exception:
                    continue
    if not bold_ok:
        _STATE['bold'] = _STATE['normal']
    pdfmetrics.registerFontFamily(
        'CJKFAM', normal=_STATE['normal'], bold=_STATE['bold'],
        italic=_STATE['normal'], boldItalic=_STATE['bold'])


def build_styles():
    # 注意: fontName 必须是已注册的实际字体名（如 CJKREG），
    # registerFontFamily 会建立 <b>/<i> 到粗体字体的映射
    fam = _STATE['normal']
    n = _STATE['normal']
    S = {
        'title': ParagraphStyle('title', fontName=fam, fontSize=19, leading=27,
                                textColor=DARK, spaceAfter=4),
        'h1': ParagraphStyle('h1', fontName=fam, fontSize=14.5, leading=21,
                             textColor=HexColor('#0F766E'), spaceBefore=16, spaceAfter=4),
        'h2': ParagraphStyle('h2', fontName=fam, fontSize=12, leading=18,
                             textColor=HexColor('#115E59'), spaceBefore=10, spaceAfter=3),
        'h3': ParagraphStyle('h3', fontName=fam, fontSize=10.8, leading=16,
                             textColor=DARK, spaceBefore=8, spaceAfter=2),
        'body': ParagraphStyle('body', fontName=fam, fontSize=10.5, leading=17,
                               textColor=HexColor('#111827'), spaceAfter=4),
        'quote': ParagraphStyle('quote', fontName=fam, fontSize=9.8, leading=15.5,
                                textColor=GRAY),
        'caption': ParagraphStyle('caption', fontName=fam, fontSize=9, leading=13,
                                  textColor=GRAY, alignment=TA_CENTER,
                                  spaceBefore=2, spaceAfter=8),
        'cell': ParagraphStyle('cell', fontName=fam, fontSize=9, leading=13.5),
        'cellh': ParagraphStyle('cellh', fontName=fam, fontSize=9.2, leading=13.5,
                                textColor=colors.white),
    }
    for lv in range(5):
        S['b%d' % lv] = ParagraphStyle(
            'b%d' % lv, fontName=fam, fontSize=10.5, leading=16.5,
            textColor=HexColor('#111827'), spaceAfter=3,
            leftIndent=14 + 18 * lv, bulletIndent=4 + 18 * lv,
            bulletFontName=n, bulletFontSize=10.5)
    return S


def make_image(path: str, alt: str, base_dir: str, usable_w, styles):
    full = path if os.path.isabs(path) else os.path.join(base_dir, path)
    flows = []
    if not os.path.exists(full):
        flows.append(Paragraph('（图片缺失：%s）' % esc(sanitize(path)), styles['quote']))
        return KeepTogether(flows)
    try:
        ir = ImageReader(full)
        iw, ih = ir.getSize()
    except Exception:
        flows.append(Paragraph('（图片无法读取：%s）' % esc(path), styles['quote']))
        return KeepTogether(flows)
    w = min(usable_w * 0.92, iw)
    h = ih * w / iw
    img = Image(full, width=w, height=h)
    img.hAlign = 'CENTER'
    flows.append(img)
    cap = sanitize(alt).strip()
    if cap:
        flows.append(Paragraph(esc(cap), styles['caption']))
    return KeepTogether(flows)


def build_table(tbl_lines, styles, usable_w):
    rows = []
    for ln in tbl_lines:
        if re.match(r'^\|?\s*:?-{2,}', ln):  # 分隔行
            continue
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        if any(cells):
            rows.append(cells)
    if not rows:
        return Spacer(1, 2)
    ncols = max(len(r) for r in rows)
    rows = [r + [''] * (ncols - len(r)) for r in rows]
    weights = [max(max(len(r[ci]) for r in rows), 4) for ci in range(ncols)]
    total = sum(weights)
    col_ws = [usable_w * w / total for w in weights]
    data = []
    for ri, r in enumerate(rows):
        st = styles['cellh'] if ri == 0 else styles['cell']
        data.append([Paragraph(inline_md(c) or ' ', st) for c in r])
    t = Table(data, colWidths=col_ws, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#0F766E')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, HexColor('#F0FDFA')]),
        ('GRID', (0, 0), (-1, -1), 0.5, LINEC),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return t


def make_quote(q_lines, styles, usable_w):
    text = '<br/>'.join(inline_md(x) for x in q_lines if x)
    t = Table([[Paragraph(text, styles['quote'])]], colWidths=[usable_w - 6])
    t.setStyle(TableStyle([
        ('LINEBEFORE', (0, 0), (0, -1), 2.2, ACCENT),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    return t


def md_to_story(md_text, base_dir, styles, usable_w):
    story = []
    lines = md_text.splitlines()
    i, n = 0, len(lines)
    title_used = False
    while i < n:
        line = lines[i].rstrip()
        s = line.strip()
        if not s:
            story.append(Spacer(1, 3))
            i += 1
            continue
        # 代码块
        if s.startswith('```'):
            block = []
            i += 1
            while i < n and not lines[i].strip().startswith('```'):
                block.append(lines[i])
                i += 1
            i += 1
            code = '\n'.join(block)
            if code.strip():
                pre = Preformatted(code, ParagraphStyle(
                    'code', fontName='Courier', fontSize=8.5, leading=11.5,
                    textColor=HexColor('#334155')))
                t = Table([[pre]], colWidths=[usable_w])
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), HexColor('#F1F5F9')),
                    ('BOX', (0, 0), (-1, -1), 0.5, LINEC),
                    ('LEFTPADDING', (0, 0), (-1, -1), 8),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                    ('TOPPADDING', (0, 0), (-1, -1), 5),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ]))
                story.append(t)
                story.append(Spacer(1, 6))
            continue
        # 表格
        if s.startswith('|'):
            tbl_lines = []
            while i < n and lines[i].strip().startswith('|'):
                tbl_lines.append(lines[i].strip())
                i += 1
            story.append(build_table(tbl_lines, styles, usable_w))
            story.append(Spacer(1, 6))
            continue
        # 标题
        m = re.match(r'^(#{1,6})\s+(.*)$', s)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1 and not title_used:
                story.append(Paragraph(inline_md(text), styles['title']))
                story.append(HRFlowable(width='100%', thickness=1.2, color=ACCENT, spaceAfter=6))
                title_used = True
            elif level == 2:
                story.append(Paragraph(inline_md(text), styles['h1']))
                story.append(HRFlowable(width='100%', thickness=0.7, color=LINEC, spaceAfter=4))
            elif level == 3:
                story.append(Paragraph(inline_md(text), styles['h2']))
            else:
                story.append(Paragraph(inline_md(text), styles['h3']))
            i += 1
            continue
        # 分隔线
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', s):
            story.append(HRFlowable(width='100%', thickness=0.6, color=LINEC,
                                    spaceBefore=4, spaceAfter=6))
            i += 1
            continue
        # 独立图片行
        m = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', s)
        if m:
            story.append(make_image(m.group(2), m.group(1), base_dir, usable_w, styles))
            i += 1
            continue
        # 引用块
        if s.startswith('>'):
            q = []
            while i < n and lines[i].strip().startswith('>'):
                q.append(lines[i].strip().lstrip('>').strip())
                i += 1
            story.append(make_quote(q, styles, usable_w))
            story.append(Spacer(1, 5))
            continue
        # 复选框列表
        m = re.match(r'^(\s*)([-*+])\s+\[([ xX])\]\s+(.*)$', line)
        if m:
            level = min(len(m.group(1)) // 2, 4)
            mark = '√ ' if m.group(3).lower() == 'x' else '□ '
            story.append(Paragraph(mark + inline_md(m.group(4)), styles['b%d' % level]))
            i += 1
            continue
        # 无序列表
        m = re.match(r'^(\s*)([-*+])\s+(.*)$', line)
        if m:
            level = min(len(m.group(1)) // 2, 4)
            bullet = '•' if level == 0 else '–'
            story.append(Paragraph(inline_md(m.group(3)), styles['b%d' % level],
                                   bulletText=bullet))
            i += 1
            continue
        # 有序列表
        m = re.match(r'^(\s*)(\d+)[.、)]\s+(.*)$', line)
        if m:
            level = min(len(m.group(1)) // 2, 4)
            story.append(Paragraph(inline_md(m.group(3)), styles['b%d' % level],
                                   bulletText=m.group(2) + '.'))
            i += 1
            continue
        # 普通段落
        story.append(Paragraph(inline_md(s), styles['body']))
        i += 1
    return story


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(_STATE['normal'], 8.5)
    canvas.setFillColor(GRAY)
    canvas.drawCentredString(A4[0] / 2, 1.1 * cm, '— %d —' % doc.page)
    canvas.restoreState()


def first_h1(md_text):
    m = re.search(r'^#\s+(.+)$', md_text, re.M)
    return sanitize(m.group(1)).strip() if m else ''


def main():
    ap = argparse.ArgumentParser(description='Markdown 网课笔记转 PDF')
    ap.add_argument('input', help='输入 Markdown 文件')
    ap.add_argument('-o', '--output', default=None, help='输出 PDF 路径')
    ap.add_argument('--title', default=None, help='PDF 文档标题（默认取首个一级标题）')
    ap.add_argument('--img-base', default=None, help='图片相对路径基准目录（默认 md 所在目录）')
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print('错误: 找不到输入文件 %s' % args.input)
        sys.exit(1)
    with open(args.input, 'r', encoding='utf-8') as f:
        md_text = f.read()
    base_dir = args.img_base or os.path.dirname(os.path.abspath(args.input))
    out = args.output or os.path.splitext(args.input)[0] + '.pdf'

    register_fonts()
    styles = build_styles()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2.2 * cm, bottomMargin=2 * cm,
        title= sanitize(args.title or first_h1(md_text) or '网课重点笔记'),
        author='TeleAgent · 网课重点笔记大师')
    usable_w = A4[0] - 4 * cm
    story = md_to_story(md_text, base_dir, styles, usable_w)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print('已生成: %s (%.1f KB, 共 %d 页)' % (out, os.path.getsize(out) / 1024, doc.page))


if __name__ == '__main__':
    main()