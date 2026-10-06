#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_mindmap.py — 知识图谱 / Markdown 大纲 → 思维导图 PNG

用法:
    python gen_mindmap.py knowledge_map.json [-o mindmap.png] [--dpi 200]
    python gen_mindmap.py outline.md [-o mindmap.png]

依赖:
    python -m pip install matplotlib
    中文字体自动使用系统微软雅黑/黑体，Windows 开箱即用

输入格式:
    A) JSON（第三轮 knowledge_map 产物）:
       {"course_title":"..","chapters":[{"title":"..","points":[{"title":".."}]}]}
    B) Markdown 缩进大纲: 顶层列表项为根，缩进 2 空格加一级
"""
import argparse
import colorsys
import json
import os
import re
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, PathPatch
from matplotlib.path import Path

plt.rcParams['font.sans-serif'] = [
    'Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'Source Han Sans SC', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 布局常量（单位: pt，导出 dpi 控制清晰度）
MARGIN = 28
COL_GAP = 62
V_GAP = 12
PAD_X = 11
LINE_FACTOR = 1.5
LEVEL_CFG = {0: (17, 340), 1: (13.5, 300), 2: (12, 340)}
FALLBACK_CFG = (11, 300)
ROOT_BG = '#0F172A'
TEXT_DARK = '#0F172A'
TEXT_MID = '#334155'

EMOJI_RANGES = [
    (0x1F000, 0x1FAFF), (0x2600, 0x26FF), (0x2700, 0x27BF),
    (0xFE00, 0xFE0F), (0x200D, 0x200D),
]


def sanitize(text: str) -> str:
    return re.sub(r' {2,}', ' ', ''.join(
        ch for ch in str(text)
        if not any(a <= ord(ch) <= b for a, b in EMOJI_RANGES)))


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return cp >= 0x2E80 or (0xFF00 <= cp <= 0xFFEF)


def char_w(ch: str, fs: float) -> float:
    return fs if is_cjk(ch) else fs * 0.58


def wrap(text: str, fs: float, box_w: float):
    """按框宽断行，返回行列表"""
    limit = box_w - 2 * PAD_X
    lines, cur, curw = [], '', 0.0
    for ch in text:
        w = char_w(ch, fs)
        if curw + w > limit and cur:
            lines.append(cur)
            cur, curw = '', 0.0
        cur += ch
        curw += w
    if cur:
        lines.append(cur)
    return lines or ['']


class Node:
    __slots__ = ('text', 'depth', 'children', 'lines', 'w', 'h', 'x', 'y', 'fs')

    def __init__(self, text, depth):
        self.text = sanitize(text).strip() or '（空）'
        self.depth = depth
        self.children = []
        fs, maxw = LEVEL_CFG.get(depth, FALLBACK_CFG)
        self.fs = fs
        self.lines = wrap(self.text, fs, maxw)
        self.w = max(sum(char_w(c, fs) for c in ln) for ln in self.lines) + 2 * PAD_X
        self.h = len(self.lines) * fs * LINE_FACTOR + 12
        self.x = self.y = 0.0


def load_json(data):
    root = Node(data.get('course_title') or '课程知识图谱', 0)
    for ch in data.get('chapters', []):
        cnode = Node(str(ch.get('title', '')), 1)
        for p in ch.get('points', []):
            if isinstance(p, dict):
                title = str(p.get('title', ''))
            else:
                title = str(p)
            cnode.children.append(Node(title, 2))
        if cnode.children or cnode.text != '（空）':
            root.children.append(cnode)
    return root


def load_outline(content):
    root = None
    stack = []
    for line in content.splitlines():
        m = re.match(r'^(\s*)[-*+]\s+(.+)$', line)
        if not m:
            continue
        indent = len(m.group(1).replace('\t', '  '))
        depth = indent // 2
        text = re.sub(r'\*\*|`', '', m.group(2)).strip()
        if root is None:
            root = Node(text, 0)
            stack = [root]
            continue
        if depth >= len(stack):
            depth = len(stack) - 1
        while len(stack) > depth + 1:
            stack.pop()
        node = Node(text, depth + 1)
        stack[depth].children.append(node)
        stack.append(node)
    return root


def load_tree(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if path.lower().endswith('.json') or content.lstrip().startswith('{'):
        return load_json(json.loads(content))
    root = load_outline(content)
    if root is None:
        raise ValueError('大纲中未找到列表项，请检查输入文件')
    return root


def layout(root):
    """后序遍历分配 y（叶子均匀占位，父节点居中于子节点），按层级分列分配 x"""
    cursor = [0.0]

    def assign_y(node):
        if not node.children:
            node.y = cursor[0] + node.h / 2
            cursor[0] += node.h + V_GAP
        else:
            for c in node.children:
                assign_y(c)
            node.y = (node.children[0].y + node.children[-1].y) / 2

    assign_y(root)

    nodes = []

    def collect(node):
        nodes.append(node)
        for c in node.children:
            collect(c)

    collect(root)
    depths = sorted({n.depth for n in nodes})
    col_start, cur = {}, MARGIN
    for d in depths:
        col_start[d] = cur
        cur += max(n.w for n in nodes if n.depth == d) + COL_GAP
    for n in nodes:
        n.x = col_start[n.depth]

    min_y = min(n.y - n.h / 2 for n in nodes)
    max_y = max(n.y + n.h / 2 for n in nodes)
    total_w = cur - COL_GAP + MARGIN
    total_h = (max_y - min_y) + 2 * MARGIN
    return nodes, total_w, total_h, min_y


def hue_color(i, s, v):
    h = (i * 0.6180339887) % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (r, g, b)


def draw_edge(ax, p, c, color):
    x0, y0 = p.x + p.w, p.y
    x1, y1 = c.x, c.y
    dx = x1 - x0
    path = Path(
        [(x0, y0), (x0 + dx * 0.45, y0), (x1 - dx * 0.45, y1), (x1, y1)],
        [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4])
    ax.add_patch(PathPatch(path, fill=False, edgecolor=color, lw=1.8,
                           alpha=0.85, capstyle='round'))


def draw_box(ax, node, bg, ec, tc, lw=1.2):
    ax.add_patch(FancyBboxPatch(
        (node.x, node.y - node.h / 2), node.w, node.h,
        boxstyle='round,pad=0,rounding_size=6',
        facecolor=bg, edgecolor=ec, lw=lw))
    ax.text(node.x + node.w / 2, node.y, '\n'.join(node.lines),
            ha='center', va='center', fontsize=node.fs, color=tc, linespacing=1.45)


def render(root, out, dpi):
    nodes, W, H, min_y = layout(root)
    # 平移 y 使内容顶部留出边距
    shift = MARGIN - min_y
    for n in nodes:
        n.y += shift

    fig = plt.figure(figsize=(W / 72, H / 72))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)  # y 向下增长
    ax.axis('off')

    parent_map = {}

    def build_parents(node):
        for c in node.children:
            parent_map[id(c)] = node
            build_parents(c)

    build_parents(root)

    def chapter_of(node):
        """返回节点所属的一级章节节点（自身即一级章节时返回自身）"""
        p = node
        while p is not None and p.depth != 1:
            p = parent_map.get(id(p))
        return p

    def chapter_index(node):
        idx = [id(c) for c in root.children]
        return idx.index(id(node)) if id(node) in idx else 0

    # 先画连线（颜色 = 子节点所属一级章节色）
    def draw_edges(node):
        for c in node.children:
            ch = chapter_of(c)
            color = hue_color(chapter_index(ch) if ch is not None else 0, 0.5, 0.62)
            draw_edge(ax, node, c, color)
            draw_edges(c)

    draw_edges(root)

    # 再画节点框
    for node in nodes:
        if node.depth == 0:
            draw_box(ax, node, ROOT_BG, ROOT_BG, 'white', lw=0)
        elif node.depth == 1:
            ci = chapter_index(node)
            draw_box(ax, node, hue_color(ci, 0.45, 0.90), hue_color(ci, 0.5, 0.62), TEXT_DARK)
        else:
            ch = chapter_of(node)
            ci = chapter_index(ch) if ch is not None else 0
            draw_box(ax, node, hue_color(ci, 0.28, 0.965), hue_color(ci, 0.35, 0.85),
                     TEXT_MID, lw=0.8)

    fig.savefig(out, dpi=dpi, facecolor='white')
    plt.close(fig)
    return W, H


def main():
    ap = argparse.ArgumentParser(description='知识图谱/大纲 → 思维导图 PNG')
    ap.add_argument('input', help='knowledge_map.json 或 Markdown 大纲')
    ap.add_argument('-o', '--output', default=None, help='输出 PNG 路径')
    ap.add_argument('--dpi', type=int, default=200, help='导出分辨率（默认 200）')
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print('错误: 找不到输入文件 %s' % args.input)
        sys.exit(1)
    try:
        root = load_tree(args.input)
    except (ValueError, json.JSONDecodeError) as e:
        print('错误: %s' % e)
        sys.exit(1)
    if not root.children:
        print('警告: 只有根节点，无子内容，仍将输出单节点图')

    out = args.output or os.path.splitext(args.input)[0] + '_mindmap.png'
    W, H = render(root, out, args.dpi)
    print('已生成: %s (%.0f x %.0f pt, dpi=%d)' % (out, W, H, args.dpi))


if __name__ == '__main__':
    main()