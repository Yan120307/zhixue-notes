# -*- coding: utf-8 -*-
"""
llm.py 自测：起一个本地 mock 的 OpenAI 兼容服务，验证
  分块 / 章节规划 / 知识点提炼 / JSON 容错 / 并发 / 降级 / 超长文稿不再被截断
"""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BACKEND = os.path.join(REPO, "backend")
sys.path.insert(0, BACKEND)

WORK = os.environ.get("ZHIXUE_TEST_WORK") or os.path.join(HERE, "_work")
os.makedirs(WORK, exist_ok=True)
sys.path.insert(0, WORK)

import config as C

# 把配置目录指向可写位置，并配置成「本地 mock 服务」
C.DATA_DIR = os.path.join(WORK, "_work", "llmtest")
C.CONFIG_PATH = os.path.join(C.DATA_DIR, "config.json")
os.makedirs(C.DATA_DIR, exist_ok=True)
os.environ.pop("DASHSCOPE_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ZHIXUE_LLM_API_KEY", None)

import llm as L

fails = []
total = [0]


def check(name, cond, extra=""):
    total[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------------------
# mock 服务：按 prompt 内容返回不同 JSON，并故意制造若干"不规矩"的返回
# ---------------------------------------------------------------------------
CALLS = {"count": 0, "max_prompt_chars": 0, "seen_markdown_wrap": False}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        prompt = req["messages"][-1]["content"]
        CALLS["count"] += 1
        CALLS["max_prompt_chars"] = max(CALLS["max_prompt_chars"], len(prompt))

        if "章节目录" in prompt:
            # 故意用 markdown 围栏包裹，测试容错
            CALLS["seen_markdown_wrap"] = True
            content = "```json\n" + json.dumps({
                "chapters": [
                    {"title": "字幕抓取与 WBI 签名", "hint": "接口与签名"},
                    {"title": "文稿提炼与笔记生成", "hint": "提炼流程"},
                ]}, ensure_ascii=False) + "\n```"
        elif "补充通俗解释" in prompt:
            # 故意在 JSON 前后加废话，测试容错。
            # 注意：prompt 里有两处 []，必须从「知识点：」之后开始找，否则会截错区间。
            tail = prompt.split("知识点：", 1)[-1]
            items = json.loads(tail[tail.find("["):tail.rfind("]") + 1])
            content = "好的，这是结果：\n" + json.dumps({
                "items": [{"title": it["title"], "one_liner": "一句话" + it["title"][:6],
                           "plain_explain": "白话解释" + it["title"][:6]} for it in items]
            }, ensure_ascii=False) + "\n希望有帮助！"
        elif "必背考点" in prompt:
            content = json.dumps({"overview": "本课讲了字幕抓取与笔记提炼两条主线。",
                                  "exam_points": ["WBI 需要 img_key 与 sub_key",
                                                  "字幕优先选人工中文"]},
                                 ensure_ascii=False)
        elif "提炼知识点" in prompt:
            # 每块返回 2 个知识点，标题故意带换行测试清洗
            content = json.dumps({"points": [
                {"title": "第 %d 块的知识点甲" % CALLS["count"],
                 "gist": "这是要点甲的说明", "detail": "详细说明甲。第二句。",
                 "keywords": ["甲", "关键词"], "plain_explain": "白话甲"},
                {"title": "第 %d 块的知识点乙" % CALLS["count"],
                 "gist": "这是要点乙的说明", "detail": "详细说明乙。",
                 "keywords": ["乙"]},
            ]}, ensure_ascii=False)
        elif "只回复两个字" in prompt:
            content = "正常"
        else:
            content = "{}"

        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("mock 服务端口:", port)

C.set_llm(provider="ollama", base_url="http://127.0.0.1:%d/v1" % port,
          model="mock-model", api_key="")
p = C.provider_of()
check("mock 配置生效", p is not None and p["is_local"], p)

print("\n1) 连通测试")
ok, msg = L.test_connection()
check("test_connection 成功", ok, msg)

print("\n2) JSON 容错解析")
check("markdown 围栏", L.parse_json_loose('```json\n{"a":1}\n```') == {"a": 1})
check("前后带废话", L.parse_json_loose('好的：{"a":2} 完毕') == {"a": 2})
check("纯数组", L.parse_json_loose('[{"a":3}]') == [{"a": 3}])
check("坏 JSON 返回 None", L.parse_json_loose('{ not json ') is None)
check("空输入返回 None", L.parse_json_loose('') is None)

print("\n3) 分块逻辑：超长文稿必须被切块，而不是被截断")
# 造 30 分钟视频、每 10 秒一条、共 180 条，总长度远超旧的 3000/4000 字上限
items = []
for i in range(180):
    items.append((i * 10, "这是第%d条字幕内容，讲解了一个具体的知识点，长度大约三十个字。" % i))
full_chars = sum(len(t) for _, t in items)
chunks = L._split_chunks(items)
print("     全文 %d 字 -> %d 块" % (full_chars, len(chunks)))
check("文稿长度确实超过旧上限 4000", full_chars > 4000, full_chars)
check("被切成多块", len(chunks) > 1, len(chunks))
check("切块后总字数与原文一致", sum(len(c["text"]) for c in chunks) >= full_chars * 0.95)
check("每块不超过 4000+单条上限", all(len(c["text"]) <= 4200 for c in chunks),
      [len(c["text"]) for c in chunks])
check("块带起止时间", all("start" in c and "end" in c for c in chunks))

print("\n4) 一站式构建知识图谱（关键：覆盖全文）")
kmap = L.llm_build_knowledge_map(items, title="测试课程", max_chapters=3,
                                 points_per_chapter=5, quality="standard")
check("返回了知识图谱", isinstance(kmap, dict), type(kmap))
check("标记为 LLM 生成", kmap and kmap.get("llm_generated") is True)
check("章节数在规划范围内", kmap and 1 <= len(kmap["chapters"]) <= 3,
      kmap and len(kmap["chapters"]))
point_titles = [p["title"] for ch in kmap["chapters"] for p in ch["points"]]
check("有知识点", len(point_titles) > 0, len(point_titles))
check("知识点标题不是截断句子（不含'这是第'）",
      not any("这是第" in t for t in point_titles), point_titles[:3])
check("带 time 轴（视频场景）",
      all(p.get("start_label") for ch in kmap["chapters"] for p in ch["points"]))
check("章节标题来自 LLM 规划",
      any("WBI" in ch["title"] for ch in kmap["chapters"]),
      [ch["title"] for ch in kmap["chapters"]])
check("发了多次请求（说明确实分块处理）", CALLS["count"] > 3, CALLS["count"])

print("\n5) 通俗解释增强")
res = L.llm_enhance_note(kmap)
check("返回增强后的 kmap", res is not None)
with_ol = [p for ch in res["chapters"] for p in ch["points"] if p.get("one_liner")]
check("部分知识点有 one_liner", len(with_ol) > 0, len(with_ol))
check("标记 llm_enhanced", res.get("llm_enhanced") is True)

print("\n6) 概述 + 考点")
s = L.llm_generate_summary(" ".join(t for _, t in items))
check("overview 非空", s and s.get("overview"), s)
check("exam_points 非空", s and len(s["exam_points"]) >= 1, s)

print("\n7) 不可用时优雅降级")
C.save_config({"llm": {"enabled": False}})
check("llm_available False", L.llm_available() is False)
check("llm_chat 返回 None", L.llm_chat([{"role": "user", "content": "x"}]) is None)
check("build_knowledge_map 返回 None（让上层降级）",
      L.llm_build_knowledge_map(items) is None)
check("enhance_note 返回 None", L.llm_enhance_note({"chapters": [{"title": "t", "points": [{"title": "p"}]}]}) is None)

print("\n8) 连接失败时不抛异常")
C.set_llm(provider="ollama", base_url="http://127.0.0.1:1/v1", model="dead", api_key="")
C.save_config({"llm": {"max_retries": 0, "timeout": 5}})
ok2, msg2 = L.test_connection()
check("失败返回 False 而不是抛异常", ok2 is False, msg2)
check("失败信息可读", "连接失败" in msg2, msg2)

srv.shutdown()
print("\n" + "=" * 50)
print("结果：%d 项检查，%d 项通过，%d 项失败" %
      (total[0], total[0] - len(fails), len(fails)))
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("llm.py 全部自测通过")
