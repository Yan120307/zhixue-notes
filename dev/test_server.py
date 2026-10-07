# -*- coding: utf-8 -*-
"""
server.py 集成自测：
  A. 纯函数：make_title / keyword_scores / basic_extract / build_knowledge_map
  B. 真启动 HTTP 服务，验证 /api/config、/api/config/save、/api/llm/test、/api/notes 等
  C. 端到端整理：纯文本输入 -> 生成 notes.md（走启发式提炼，不需要大模型）
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BACKEND = os.path.join(REPO, "backend")
WORK = os.environ.get("ZHIXUE_TEST_WORK") or os.path.join(HERE, "_work")
os.makedirs(WORK, exist_ok=True)
sys.path.insert(0, BACKEND)

# 让服务把数据写在我的可写目录，避免污染用户 ~/.zhixue-notes
DATA = os.path.join(WORK, "_work", "serverdata")
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(DATA, exist_ok=True)

fails = []
total = [0]


def check(name, cond, extra=""):
    total[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
print("A. 纯函数层（直接 import server，不启服务）")
# 用环境变量把 DATA_DIR 指到可写目录：server 用 expanduser("~") 拼路径，
# 所以这里改 HOME 更稳妥；同时清掉可能干扰的 LLM Key。
os.environ["USERPROFILE"] = WORK
os.environ["HOME"] = WORK
for k in ("ZHIXUE_LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ZHIXUE_LLM_BASE_URL"):
    os.environ.pop(k, None)

import server as S

check("模块导入成功", hasattr(S, "build_knowledge_map"))
check("重复函数已清理", S.__dict__.get("faster_whisper_available") is not None)

print("\n  make_title（旧版是直接截断句子，这里是真提炼）")
cases = [
    ("线性回归是一种预测连续数值的方法。", "线性回归"),
    ("注意：梯度下降需要选择合适的学习率。", "梯度下降"),
    ("这个就是 WBI 签名的核心思路，需要两个 key。", "WBI"),
]
for sent, expect_contains in cases:
    got = S.make_title(sent)
    check("「%s」-> 「%s」" % (sent[:14], got), expect_contains in got, got)

# 纯过渡句不含知识点，应当被丢弃（返回空串），由调用方跳过
check("过渡句被丢弃", S.make_title("那么，接下来我们讲一下反向传播。") == "",
      S.make_title("那么，接下来我们讲一下反向传播。"))
t = S.make_title("然后这个就是函数的定义域，需要特别注意。")
check("口语词被去掉", not t.startswith("然后"), t)
check("超长句被收短", len(S.make_title("一二三四五六七八九十" * 5)) <= 24,
      len(S.make_title("一二三四五六七八九十" * 5)))

print("\n  keyword_scores")
kws = S.keyword_scores(["梯度下降是优化算法", "梯度下降需要学习率", "学习率太大会震荡"])
check("抽出高频关键词", len(kws) > 0, list(kws)[:8])
check("'梯度下降' 被抽到", any("梯度" in w for w in kws), list(kws)[:10])

print("\n  basic_extract（启发式兜底）")
# 注意：要用"内容各不相同"的条目，否则会被 _dedup_items 当成重复句合并掉，
# 时间跨度就变成 0，时间轴判定自然为假。这里构造 40 条不同的字幕。
_concepts = ["梯度下降", "学习率", "动量法", "Adam 优化器", "批归一化", "L2 正则化",
             "早停法", "交叉验证", "损失函数", "激活函数"]
items = [(i * 10, "%s是本节课第%d个要点，它通过调整参数来影响模型的训练效果。" % (
    _concepts[i % len(_concepts)], i)) for i in range(40)]
kmap = S.basic_extract(items, title="测试课程")
titles = [p["title"] for ch in kmap["chapters"] for p in ch["points"]]
check("产出章节", len(kmap["chapters"]) > 0, len(kmap["chapters"]))
check("产出知识点", len(titles) > 0, len(titles))
check("标题不再带句末标点", not any(t.endswith(("，", "。", "、")) for t in titles), titles[:5])
check("标题不是过渡句残渣", not any(t in ("接下来", "那么", "然后") for t in titles), titles[:5])
check("识别出时间轴", kmap.get("has_timeline") is True, kmap.get("has_timeline"))
check("有 time 轴", all(p.get("start_label") for ch in kmap["chapters"] for p in ch["points"]),
      [(p.get("title"), p.get("start_label")) for ch in kmap["chapters"] for p in ch["points"]][:3])

print("\n  build_knowledge_map（未配模型时回落启发式）")
kmap2, used_llm = S.build_knowledge_map(items, title="回落测试")
check("回落成功", kmap2 and kmap2.get("chapters"), used_llm)
check("used_llm 为 False", used_llm is False)

# ---------------------------------------------------------------------------
print("\nB/C. 启动真实服务并走接口")
proc = subprocess.Popen(
    [sys.executable, os.path.join(BACKEND, "server.py"), "--port", "8899"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    cwd=BACKEND, env=dict(os.environ), text=True, encoding="utf-8", errors="ignore")
BASE = "http://127.0.0.1:8899"


def call(path, data=None, timeout=30, method=None):
    """
    明确区分 GET / POST。
    注意：urllib 在 data=None 时发的是 GET，所以 POST 接口必须显式给 body 或 method。
    """
    url = BASE + path
    try:
        if method is None:
            method = "POST" if data is not None else "GET"
        body = json.dumps(data).encode("utf-8") if data is not None else (
            b"{}" if method == "POST" else None)
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


# 等服务起来
up = False
for _ in range(40):
    time.sleep(0.5)
    st, _d = call("/api/config")
    if st == 200:
        up = True
        break
check("服务启动成功", up)
if not up:
    proc.kill()
    print("服务没起来，输出：")
    print(proc.stdout.read()[:3000])
    sys.exit(1)

print("\n  GET /api/config")
st, cfg = call("/api/config")
check("返回 200", st == 200, st)
check("带服务商目录", len(cfg.get("_provider_catalog") or []) >= 8)
check("明确未配置状态", cfg.get("llm", {}).get("active") is False)
check("带配置文件路径", "_config_path" in cfg)

print("\n  POST /api/config/save（一键接入大模型）")
st, r = call("/api/config/save", {
    "provider": "ollama", "model": "qwen2.5:7b", "api_key": "",
    "base_url": "http://127.0.0.1:11434/v1", "enabled": True})
check("保存返回 200", st == 200, (st, r))
check("保存后 active", r.get("config", {}).get("llm", {}).get("active") is True, r)
st, r2 = call("/api/config")
check("再读取仍是 ollama", r2["llm"]["provider"] == "ollama", r2["llm"])
check("base_url 正确", r2["llm"]["base_url"] == "http://127.0.0.1:11434/v1", r2["llm"])

print("\n  POST /api/config/save（打码值不能被当成真 Key 存进去）")
st, _ = call("/api/config/save", {"provider": "siliconflow",
                                  "api_key": "sk-abc123...wxyz",
                                  "model": "Qwen/Qwen2.5-7B-Instruct"})
st, r3 = call("/api/config")
check("打码值未被写入", r3["llm"]["api_key_set"] is False, r3["llm"]["api_key"])

print("\n  POST /api/llm/test（连不上要给出可读原因，不能 500 崩溃）")
st, r = call("/api/llm/test", {"provider": "ollama",
                               "base_url": "http://127.0.0.1:1/v1",
                               "model": "dead", "api_key": ""}, timeout=60)
check("接口正常返回", st in (200, 400), (st, r))
check("失败时 ok=False", r.get("ok") is False, r)
check("给出可读原因", "失败" in (r.get("message") or ""), r.get("message"))

print("\n  GET /api/models")
st, r = call("/api/models?provider=zhipu")
check("返回模型列表", st == 200 and len(r.get("models") or []) > 0, r)

print("\n  POST /api/process（纯文本端到端整理，不需要大模型）")
long_text = ("梯度下降是一种优化算法，它通过迭代更新参数来最小化损失函数。"
             "学习率决定了每次参数更新的步长，学习率过大会导致训练震荡甚至发散。"
             "动量法可以加速收敛并减少震荡，它累积历史梯度方向。"
             "自适应学习率方法如 Adam 会为每个参数单独调整步长。"
             "批归一化能缓解内部协变量偏移，从而允许使用更大的学习率。"
             "正则化通过惩罚模型复杂度来抑制过拟合，常见的有 L1 和 L2 正则。"
             "早停法在验证集性能不再提升时停止训练，也是一种正则化手段。"
             "交叉验证能更可靠地评估模型在未见数据上的泛化能力。")
st, r = call("/api/process", {"url": long_text, "fmts": ["md", "mindmap"]})
check("创建任务成功", st == 200 and r.get("task_id"), (st, r))
tid = r.get("task_id")

status = None
for _ in range(60):
    time.sleep(1)
    st, status = call("/api/status?id=" + tid)
    if status.get("done"):
        break
check("任务完成", status and status.get("done"), status and status.get("log"))
check("没有报错", not (status or {}).get("error"), (status or {}).get("error"))
res = (status or {}).get("result") or {}
check("产出了文件", "notes.md" in (res.get("files") or {}), res.get("files"))
check("有知识点", (res.get("points") or 0) > 0, res.get("points"))

print("\n  GET /api/read（在线预览）")
st, r = call("/api/read?id=%s&file=notes.md" % tid)
check("读到笔记正文", st == 200 and "梯度下降" in (r.get("content") or ""), st)
md = r.get("content") or ""
check("笔记含概述", "## 概述" in md)
check("笔记含章节", "## 第" in md or "## " in md)
check("笔记含考点清单", "重点考点清单" in md)

print("\n  GET /api/notes（历史列表）")
st, r = call("/api/notes")
check("返回列表", st == 200 and len(r.get("notes") or []) >= 1, r)
check("包含刚完成的任务", any(n["id"] == tid for n in r.get("notes", [])))
_note = next((n for n in r.get("notes", []) if n["id"] == tid), {})
check("带输出格式信息", isinstance(_note.get("fmts"), list), _note.get("fmts"))
check("带来源类型信息", bool(_note.get("type")), _note.get("type"))
check("带原始链接", "梯度下降" in (_note.get("src") or ""), _note.get("src"))

print("\n  POST /api/notes/forget（从列表移除）")
st, r = call("/api/notes/forget", {"id": "不存在的ID"})
check("不存在的 ID 也不报 500", st in (200, 400), (st, r))
st, r = call("/api/notes/forget", {"id": "../../etc"})
check("拒绝非法任务 ID", st == 400, (st, r))

print("\n  POST /api/tasks/cleanup（任务清理）")
st, r = call("/api/tasks/cleanup", {"force": False})
check("清理接口可用", st == 200 and r.get("ok"), (st, r))
check("返回清理统计", "kept" in r and "total_mb" in r, r)
# 非强制清理不应删掉刚完成的笔记（它在保留窗口内）
st, r = call("/api/notes")
check("非强制清理后笔记仍在", any(n["id"] == tid for n in r.get("notes", [])),
      [n["id"] for n in r.get("notes", [])])

print("\n  路径穿越防护")
st, r = call("/api/read?id=%s&file=../../../config.json" % tid)
check("拒绝路径穿越", st == 404, (st, r))
st, r = call("/api/files/%s/..%%2F..%%2Fconfig.json" % tid)
check("下载接口也拒绝", st == 404, (st, r))

print("\n  POST /api/config/reset")
# 注意：必须显式 method="POST"，否则 urllib 在无 body 时发 GET，会命中不到 POST 路由
st, r = call("/api/config/reset", method="POST")
check("重置成功", st == 200 and r.get("ok"), (st, r))
st, r = call("/api/config")
check("重置后回到未配置", r["llm"]["active"] is False, r["llm"])

proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()

print("\n" + "=" * 52)
print("结果：%d 项检查，%d 项通过，%d 项失败" %
      (total[0], total[0] - len(fails), len(fails)))
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("server.py 集成自测通过")
