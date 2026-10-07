#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智学笔记 · 回归测试运行器

用法：
    python dev/run_tests.py              # 跑全部
    python dev/run_tests.py config llm   # 只跑指定的几个（按文件名匹配）

退出码：0 = 全部通过，1 = 有失败

说明：
- 全部测试都不需要联网（大模型与 HTTP 交互都用本地 mock 服务替代）
- 工作目录默认 dev/_work/（已 gitignore）；可用 ZHIXUE_TEST_WORK 环境变量覆盖
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 顺序有讲究：先纯逻辑，再集成，最后端到端
SUITES = [
    ("config", "test_config.py", "配置中心：读写/环境变量优先级/打码/损坏文件容错"),
    ("netutil", "test_netutil.py", "链接解析：短链跳转/av→BV/多P/标题清洗/失败分支"),
    ("docextract", "test_docextract.py", "文档抽取：PDF/DOCX 解析、编码试探、BOM"),
    ("llm", "test_llm.py", "大模型：分块/章节规划/JSON 容错/并发/降级"),
    ("server", "test_server.py", "后端集成：全部接口 + 端到端整理 + 安全校验"),
    ("e2e", "test_e2e_llm.py", "端到端：接入大模型 → 整理 → 笔记落盘"),
]


def main():
    args = [a.lower() for a in sys.argv[1:]]
    picked = [s for s in SUITES
              if not args or any(a in s[0] or a in s[1] for a in args)]
    if not picked:
        print("没有匹配的测试。可选：", ", ".join(s[0] for s in SUITES))
        return 1

    work = os.environ.get("ZHIXUE_TEST_WORK") or os.path.join(HERE, "_work")
    os.makedirs(work, exist_ok=True)
    env = dict(os.environ)
    env["ZHIXUE_TEST_WORK"] = work
    env["PYTHONIOENCODING"] = "utf-8"
    # 避免外部 Key 干扰「未配置大模型」相关断言
    for k in ("ZHIXUE_LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY",
              "ZHIXUE_LLM_BASE_URL", "ZHIXUE_LLM_MODEL"):
        env.pop(k, None)

    print("=" * 68)
    print("智学笔记 回归测试")
    print("仓库:", REPO)
    print("工作目录:", work)
    print("=" * 68)

    results = []
    for key, fname, desc in picked:
        path = os.path.join(HERE, fname)
        if not os.path.exists(path):
            print("\n[跳过] %s（文件不存在）" % fname)
            results.append((key, 0, 0, "缺失"))
            continue
        print("\n▶ %s — %s" % (key, desc))
        t0 = time.time()
        proc = subprocess.run([sys.executable, path], cwd=HERE, env=env,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="ignore")
        out = (proc.stdout or "") + (proc.stderr or "")
        # 打印每条 PASS/FAIL，方便定位
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("PASS") or s.startswith("FAIL"):
                print("   ", s)
        passed = out.count("  PASS  ")
        failed = out.count("  FAIL  ")
        cost = time.time() - t0
        status = "通过" if proc.returncode == 0 and failed == 0 else "失败"
        if status == "失败":
            # 失败时把完整输出打出来，避免"不知道为什么失败"
            print("   --- 完整输出 ---")
            for line in out.splitlines()[-40:]:
                print("   ", line)
        print("   [%s] 通过 %d / 失败 %d（%.1fs）" % (status, passed, failed, cost))
        results.append((key, passed, failed, status))

    print("\n" + "=" * 68)
    print("汇总")
    print("=" * 68)
    total_p = sum(r[1] for r in results)
    total_f = sum(r[2] for r in results)
    for key, p, f, st in results:
        flag = "✓" if st == "通过" else ("-" if st == "缺失" else "✗")
        print("  %s %-12s 通过 %3d  失败 %3d" % (flag, key, p, f))
    print("-" * 68)
    print("  合计：通过 %d，失败 %d" % (total_p, total_f))
    if total_f == 0 and all(r[3] != "失败" for r in results):
        print("\n全部通过 ✅")
        return 0
    print("\n存在失败 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
