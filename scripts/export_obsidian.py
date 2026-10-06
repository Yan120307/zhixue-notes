#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Obsidian Vault 导出 — 把智学笔记写入 Obsidian 仓库

用法:
    python export_obsidian.py <任务目录> --vault "C:/你的Obsidian仓库路径" [--folder 智学笔记]

说明:
    1. 在 Vault 指定文件夹下创建笔记（含 wikilink 知识图谱）
    2. 笔记名带日期防重名，原 notes.md 保留完整内容
    3. 自动生成 MOC（Map of Content）索引页
"""
import argparse
import datetime
import os
import sys


def write_obsidian(task_dir, vault, folder="智学笔记"):
    """把任务目录中的笔记写入 Obsidian vault，返回写入文件列表"""
    notes_path = os.path.join(task_dir, "notes.md")
    kmap_path = os.path.join(task_dir, "knowledge_map.json")
    if not os.path.exists(notes_path):
        raise FileNotFoundError("任务目录中没有 notes.md")

    target_dir = os.path.join(vault, folder)
    os.makedirs(target_dir, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    written = []

    # 1. 主笔记
    with open(notes_path, "r", encoding="utf-8") as f:
        content = f.read()
    # 提取标题作为文件名
    title = "笔记_" + stamp
    first_line = content.split("\n", 1)[0].lstrip("# ").strip()
    if first_line:
        title = first_line.replace(" — 重点笔记", "").strip()[:40] + "_" + stamp
    # 清理文件名非法字符
    for c in '\\/:*?"<>|':
        title = title.replace(c, "_")
    main_path = os.path.join(target_dir, title + ".md")
    with open(main_path, "w", encoding="utf-8") as f:
        f.write(content)
    written.append(main_path)

    # 2. 知识图谱 wikilink 索引（Obsidian 特色）
    if os.path.exists(kmap_path):
        import json
        with open(kmap_path, "r", encoding="utf-8") as f:
            kmap = json.load(f)
        graph_name = "图谱_" + stamp + ".md"
        graph_path = os.path.join(target_dir, graph_name)
        lines = ["# 知识图谱（MOC）", "", "> 自动生成于智学笔记平台", ""]
        for ch in kmap.get("chapters", []):
            lines.append("## %s" % ch["title"])
            for p in ch.get("points", []):
                lines.append("- [[%s]] — %s" % (p["title"], p.get("one_liner", p.get("summary", ""))))
        lines.append("")
        lines.append("---")
        lines.append("主笔记: [[%s]]" % title)
        with open(graph_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        written.append(graph_path)

    return written


def main():
    ap = argparse.ArgumentParser(description="写入 Obsidian Vault")
    ap.add_argument("task_dir", help="智学笔记任务目录（含 notes.md）")
    ap.add_argument("--vault", required=True, help="Obsidian 仓库根目录路径")
    ap.add_argument("--folder", default="智学笔记", help="Vault 内的目标文件夹名")
    args = ap.parse_args()

    if not os.path.isdir(args.vault):
        print("错误: Vault 目录不存在: %s" % args.vault)
        sys.exit(1)
    try:
        written = write_obsidian(args.task_dir, args.vault, args.folder)
        print("已写入 %d 个文件:" % len(written))
        for w in written:
            print("  " + w)
    except Exception as e:
        print("失败: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()