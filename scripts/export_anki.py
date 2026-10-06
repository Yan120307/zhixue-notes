#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anki 卡片导出 — knowledge_map.json → Anki 可导入 TSV

用法:
    python export_anki.py knowledge_map.json [-o anki_cards.txt]

说明:
    生成 Anki「基本型」卡片 TSV（制表符分隔），字段: 正面(问题) \t 背面(答案)
    导入 Anki: 文件 → 导入 → 选择本文件 → 字段分隔符选「制表符」
"""
import argparse
import json
import os
import sys


def kmap_to_cards(kmap):
    """把知识图谱转为问答卡片列表"""
    cards = []
    for ch in kmap.get("chapters", []):
        ch_title = ch.get("title", "")
        for p in ch.get("points", []):
            q = "【%s】%s 是什么？请解释其核心要点。" % (ch_title, p["title"])
            parts = []
            if p.get("one_liner"):
                parts.append("一句话掌握：%s" % p["one_liner"])
            if p.get("summary"):
                parts.append("核心要点：%s" % p["summary"])
            if p.get("plain_explain"):
                parts.append("通俗解释：%s" % p["plain_explain"])
            a = "\n".join(parts) if parts else p.get("summary", p["title"])
            cards.append((q, a))
    return cards


def main():
    ap = argparse.ArgumentParser(description="knowledge_map → Anki 卡片 TSV")
    ap.add_argument("input", help="knowledge_map.json 路径")
    ap.add_argument("-o", "--output", default=None, help="输出 TSV 路径")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print("错误: 找不到 %s" % args.input)
        sys.exit(1)
    with open(args.input, "r", encoding="utf-8") as f:
        kmap = json.load(f)

    cards = kmap_to_cards(kmap)
    if not cards:
        print("警告: 无知识点，生成空卡组")
    out = args.output or os.path.splitext(args.input)[0] + "_anki.txt"
    with open(out, "w", encoding="utf-8") as f:
        for q, a in cards:
            # TSV 格式，字段内换行用 <br>（Anki 支持 HTML）
            f.write("%s\t%s\n" % (q.replace("\t", " "), a.replace("\n", "<br>").replace("\t", " ")))
    print("已生成: %s（%d 张卡片）" % (out, len(cards)))
    print("导入方法: Anki → 文件 → 导入 → 选择此文件 → 分隔符选「制表符」")


if __name__ == "__main__":
    main()