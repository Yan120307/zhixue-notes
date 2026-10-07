# -*- coding: utf-8 -*-
"""
端到端验证：模拟「用户在设置页接入大模型 → 整理一份讲义 → 笔记里出现
真正的知识点标题 + 一句话掌握 + 通俗解释 + 必背考点」。
用一个本地 mock 的 OpenAI 兼容服务充当大模型。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BACKEND = os.path.join(REPO, "backend")
WORK = os.environ.get("ZHIXUE_TEST_WORK") or os.path.join(HERE, "_work")
os.makedirs(WORK, exist_ok=True)
sys.path.insert(0, BACKEND)

# 隔离数据目录，避免污染用户真实笔记
for k in ("ZHIXUE_LLM_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(k, None)
os.environ["USERPROFILE"] = WORK
os.environ["HOME"] = WORK
shutil.rmtree(os.path.join(WORK, ".zhixue-notes"), ignore_errors=True)

fails = []
total = [0]


def check(name, cond, extra=""):
    total[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
# mock 大模型：返回结构化的、明显"像人写的"内容，用来验证落盘效果
# ---------------------------------------------------------------------------
class MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        prompt = req["messages"][-1]["content"]

        if "章节目录" in prompt:
            content = json.dumps({"chapters": [
                {"title": "梯度下降与学习率", "hint": "优化基础"},
                {"title": "正则化与泛化", "hint": "抑制过拟合"}]}, ensure_ascii=False)
        elif "提炼知识点" in prompt:
            content = json.dumps({"points": [
                {"title": "梯度下降的迭代更新机制", "gist": "靠负梯度方向逐步逼近最小值",
                 "detail": "每次沿当前点梯度的反方向前进一小步。步长由学习率决定。",
                 "keywords": ["梯度下降", "学习率"], "plain_explain": "像下山时每次都朝最陡的下坡方向迈一步。"},
                {"title": "学习率过大导致训练发散", "gist": "步长太大会跨过最低点",
                 "detail": "步长超过临界值后损失不再下降反而增大。",
                 "keywords": ["学习率"], "plain_explain": "像下楼梯一次跨三阶，容易直接摔到楼下。"}]},
                ensure_ascii=False)
        elif "补充通俗解释" in prompt:
            content = json.dumps({"items": [
                {"title": "梯度下降的迭代更新机制", "one_liner": "朝下坡方向一步步走近最低点",
                 "plain_explain": "把它想成蒙眼下山：每一步都摸一下哪边最陡，就往那边迈一小步。"}]},
                ensure_ascii=False)
        elif "必背考点" in prompt:
            content = json.dumps({
                "overview": "本课讲清梯度下降的迭代机制，以及学习率与正则化对训练的影响。",
                "exam_points": ["梯度下降沿负梯度方向更新参数", "学习率过大会导致训练发散",
                                "L2 正则化通过惩罚权重抑制过拟合"]}, ensure_ascii=False)
        else:
            content = "正常"

        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


mock = ThreadingHTTPServer(("127.0.0.1", 0), MockLLM)
mock_port = mock.server_address[1]
threading.Thread(target=mock.serve_forever, daemon=True).start()
print("mock 大模型端口:", mock_port)

# ---------------------------------------------------------------------------
proc = subprocess.Popen(
    [sys.executable, os.path.join(BACKEND, "server.py"), "--port", "8911"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=BACKEND,
    text=True, encoding="utf-8", errors="ignore", env=dict(os.environ))
BASE = "http://127.0.0.1:8911"


def call(path, data=None, method=None, timeout=90):
    method = method or ("POST" if data is not None else "GET")
    body = json.dumps(data).encode() if data is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
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


up = False
for _ in range(40):
    time.sleep(0.5)
    if call("/api/config")[0] == 200:
        up = True
        break
check("后端启动", up)
if not up:
    proc.kill()
    print(proc.stdout.read()[:2000])
    sys.exit(1)

print("\n1) 在设置页接入大模型（等价于前端点『保存并测试』）")
st, r = call("/api/config/save", {
    "provider": "ollama", "base_url": "http://127.0.0.1:%d/v1" % mock_port,
    "model": "mock-qwen", "api_key": "", "enabled": True, "quality": "standard"})
check("保存成功", st == 200 and r.get("config", {}).get("llm", {}).get("active"), r)
st, r = call("/api/llm/test", {})
check("连接测试通过", r.get("ok") is True, r)

print("\n2) 整理一份讲义（走大模型提炼）")
lecture = ("梯度下降是一种优化算法，它通过迭代更新参数来最小化损失函数。"
           "每次更新沿着当前点梯度的反方向前进一小步。"
           "学习率决定了每次参数更新的步长，学习率过大会导致训练震荡甚至发散。"
           "动量法累积历史梯度方向，可以加速收敛并减少震荡。"
           "L2 正则化通过惩罚权重的平方和来抑制过拟合，让模型泛化更好。"
           "交叉验证能更可靠地评估模型在未见数据上的表现。")
st, r = call("/api/process", {"url": lecture, "fmts": ["md"]})
check("任务创建成功", st == 200 and r.get("task_id"), (st, r))
tid = r["task_id"]

status = None
for _ in range(90):
    time.sleep(1)
    st, status = call("/api/status?id=" + tid)
    if status.get("done"):
        break
check("任务完成", status and status.get("done"), status and status.get("log"))
check("无错误", not (status or {}).get("error"), (status or {}).get("error"))

res = (status or {}).get("result") or {}
files = res.get("files") or {}
st, md = call("/api/read?id=%s&file=notes.md" % tid)
content = md.get("content") or ""
print("\n3) 笔记内容检查")
check("含大模型生成的章节标题", "梯度下降与学习率" in content,
      [l for l in content.split("\n") if l.startswith("## ")][:4])
check("含大模型生成的知识点标题", "梯度下降的迭代更新机制" in content)
# 注意：原文句子出现在「原文摘录」里是**正确行为**（摘录就该是原文），
# 所以这里只断言「知识点标题（### 行）」不是原文截断。
headings = [l.strip() for l in content.split("\n") if l.strip().startswith("### ")]
point_headings = [h for h in headings if h.startswith("### ") and
                  "必背考点" not in h and "核心概念" not in h]
check("知识点标题不是原文截断",
      not any("是一种优化算法" in h for h in point_headings), point_headings)
check("原文确实被引用为摘录", "梯度下降是一种优化算法" in content)
check("含一句话掌握", "一句话掌握" in content and "朝下坡方向" in content)
check("含通俗解释", "通俗解释" in content and "蒙眼下山" in content)
check("含 AI 概述", "本课讲清梯度下降" in content)
check("含 AI 必背考点", "必背考点" in content and "L2 正则化通过惩罚权重" in content)
check("标注 llm_enhanced", res.get("llm_enhanced") is True, res)
check("知识点数量合理", (res.get("points") or 0) >= 2, res.get("points"))

print("\n4) 关掉 AI 后仍能出笔记（降级不报错）")
st, _ = call("/api/config/save", {"enabled": False})
check("关闭成功", st == 200)
st, r = call("/api/process", {"url": lecture, "fmts": ["md"]})
tid2 = r.get("task_id")
status2 = None
for _ in range(60):
    time.sleep(1)
    st, status2 = call("/api/status?id=" + tid2)
    if status2.get("done"):
        break
check("降级后仍完成", status2 and status2.get("done"), status2 and status2.get("log"))
check("降级后没有报错", not (status2 or {}).get("error"), (status2 or {}).get("error"))
check("降级后仍产出笔记", "notes.md" in ((status2 or {}).get("result") or {}).get("files", {}))

print("\n5) 笔记列表（含重启后的历史重建）")
st, r = call("/api/notes")
ids = [n["id"] for n in r.get("notes", [])]
check("两个任务都在列表里", tid in ids and tid2 in ids, ids)

proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()
mock.shutdown()

print("\n" + "=" * 52)
print("结果：%d 项检查，%d 项通过，%d 项失败" %
      (total[0], total[0] - len(fails), len(fails)))
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("端到端验证通过")
