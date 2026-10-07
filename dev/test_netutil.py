# -*- coding: utf-8 -*-
"""
netutil.py 自测：不联网，验证链接解析的所有分支
（用 monkeypatch 顶掉网络请求，专测解析逻辑）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BACKEND = os.path.join(REPO, "backend")
sys.path.insert(0, BACKEND)

try:
    import requests                      # noqa: F401
    HAVE_REQ = True
except ImportError:
    # 未安装 requests 时用最小替身，保证纯解析逻辑仍可测试
    HAVE_REQ = False
    import types
    _m = types.ModuleType("requests")
    _m.Session = lambda: types.SimpleNamespace(headers={}, get=lambda *a, **k: None)
    _m.exceptions = types.SimpleNamespace(Timeout=Exception, ConnectionError=Exception)
    sys.modules["requests"] = _m

import netutil as N

fails = []
total = [0]


def check(name, cond, extra=""):
    total[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


print("requests 可用:", HAVE_REQ)

# ---------------------------------------------------------------------------
print("\n1) 平台识别")
check("b23.tv 短链被识别为 B 站", N.is_bilibili("https://b23.tv/abc123"))
check("手机版 B 站被识别", N.is_bilibili("https://m.bilibili.com/video/BV1xx411c7mD"))
check("short 链接判定", N.is_short_link("https://b23.tv/abc"))
check("完整链接不是短链", not N.is_short_link("https://www.bilibili.com/video/BV1xx411c7mD"))
check("YouTube youtu.be", N.is_youtube("https://youtu.be/dQw4w9WgXcQ"))
check("YouTube shorts", N.is_youtube("https://www.youtube.com/shorts/abc123def"))

# ---------------------------------------------------------------------------
print("\n2) 从各种输入里抠 BV / av 号")
check("标准链", N.parse_bilibili_input("https://www.bilibili.com/video/BV1xx411c7mD")[0]
      == "BV1xx411c7mD")
check("带跟踪参数", N.parse_bilibili_input(
    "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333.999&vd_source=abc")[0]
    == "BV1xx411c7mD")
check("分享文案里夹链接", N.parse_bilibili_input(
    "【标题】 https://b23.tv/xyz 复制打开APP")[0] is None)
check("av 号", N.parse_bilibili_input("https://www.bilibili.com/video/av170001")[1] == "170001")
check("裸 BV 号", N.parse_bilibili_input("BV1xx411c7mD")[0] == "BV1xx411c7mD")

# ---------------------------------------------------------------------------
print("\n3) av -> BV 换算（对照 B 站官方算法已知值）")
# 前两个是与外部公开实现核对过的已知对照；av1 的值由本算法导出
cases = [("170001", "BV17x411w7KC"), ("455017605", "BV1Q541167Qg"),
         ("1", "BV1xx411c7mQ")]
for av, expect in cases:
    got = N.av_to_bvid(av)
    check("av%s -> %s" % (av, expect), got == expect, got)
# 反向校验：BV 号必须符合 BV + 10 位的格式
check("输出格式合法", all(len(N.av_to_bvid(str(i))) == 12 and
                       N.av_to_bvid(str(i)).startswith("BV") for i in range(1, 30)))

# ---------------------------------------------------------------------------
print("\n4) ?p=N 分 P 解析（多 P 视频，旧版完全忽略）")
check("无 p 参数 -> 1", N.parse_page_param("https://www.bilibili.com/video/BV1xx411c7mD") == 1)
check("?p=5", N.parse_page_param("https://www.bilibili.com/video/BV1xx411c7mD?p=5") == 5)
check("?p=0 纠正为 1", N.parse_page_param("https://x/BV1xx411c7mD?p=0") == 1)
check("?p=abc 纠正为 1", N.parse_page_param("https://x/BV1xx411c7mD?p=abc") == 1)
check("p 与其它参数混排",
      N.parse_page_param("https://x/BV1?p=3&spm_id_from=1") == 3)

# ---------------------------------------------------------------------------
print("\n5) 标题清洗")
html = "<html><head><title>Python 教程_哔哩哔哩_bilibili</title></head></html>"
check("去掉站名后缀", N.extract_title(html) == "Python 教程", N.extract_title(html))
html2 = '<meta property="og:title" content="WBI 签名详解"><title>别的</title>'
check("og:title 优先", N.extract_title(html2) == "WBI 签名详解", N.extract_title(html2))
check("空 HTML", N.extract_title("") == "")
check("YouTube 后缀", N.extract_title("<title>Hello - YouTube</title>") == "Hello",
      N.extract_title("<title>Hello - YouTube</title>"))

# ---------------------------------------------------------------------------
print("\n6) resolve_* 的失败路径必须给出可读原因（而不是抛异常）")
r = N.resolve_bilibili("https://example.com/foo")
check("非 B 站链接明确报错", r["ok"] is False and "不是 B 站" in r["error"], r)
r = N.resolve_youtube("https://www.youtube.com/feed/subscriptions")
check("频道页明确报错", r["ok"] is False and r["error"], r)
r = N.resolve_youtube("https://youtu.be/dQw4w9WgXcQ")
check("youtu.be 取到 video_id", r["ok"] and r["video_id"] == "dQw4w9WgXcQ", r)
r = N.resolve_youtube("https://www.youtube.com/watch?v=abc123&list=PLxyz")
check("带播放列表也取到 video_id", r["ok"] and r["video_id"] == "abc123" and r["list_id"] == "PLxyz", r)

# ---------------------------------------------------------------------------
print("\n7) 短链跟随跳转（mock 掉网络，验证确实走了 follow 逻辑）")
called = {"url": None}


def fake_follow(url, timeout=15):
    called["url"] = url
    return "https://www.bilibili.com/video/BV1xx411c7mD"


_orig_follow = N.follow_short_link
_orig_api = N._api
N.follow_short_link = fake_follow
N._api = lambda *a, **k: {"code": 0, "data": {
    "title": "测试视频", "duration": 600,
    "pages": [{"cid": 111, "part": "P1", "duration": 600},
              {"cid": 222, "part": "P2", "duration": 700}]}}

r = N.resolve_bilibili("https://b23.tv/短链测试")
check("短链触发了跟随跳转", called["url"] == "https://b23.tv/短链测试", called)
check("短链解析出 bvid", r["ok"] and r["bvid"] == "BV1xx411c7mD", r)
check("默认取第 1 P", r["cid"] == 111 and r["page_index"] == 1, r)
check("总 P 数正确", r["total_pages"] == 2, r)
check("标题正确", r["title"] == "测试视频", r)

r = N.resolve_bilibili("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
check("?p=2 取到第 2 P 的 cid", r["ok"] and r["cid"] == 222 and r["page_index"] == 2, r)

r = N.resolve_bilibili("https://www.bilibili.com/video/BV1xx411c7mD?p=99")
check("p 超范围时夹到最后一 P", r["page_index"] == 2 and r["cid"] == 222, r)

N._api = lambda *a, **k: {"code": -404, "message": "啥都木有"}
r = N.resolve_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
check("视频失效给友好提示", r["ok"] is False and "失效" in r["error"], r)

N._api = lambda *a, **k: {"code": -403, "message": "访问权限不足"}
r = N.resolve_bilibili("https://www.bilibili.com/video/BV1xx411c7mD")
check("权限受限给友好提示", r["ok"] is False and "访问限制" in r["error"], r)

N._api = _orig_api
N.follow_short_link = _orig_follow

# ---------------------------------------------------------------------------
print("\n8) 阅读器文本切段")
md = ("Title: 某文章\n\nURL Source: x\n\nMarkdown Content:\n"
      "# 一级标题\n\n这是第一段正文，长度足够。\n\n"
      "![图片](http://a.png)\n\n这是第二段正文，长度也足够。\n"
      "\n[导航链接](http://b)\n")
paras = N.split_paragraphs(md)
check("切出正文段落", len(paras) >= 2, paras)
check("滤掉图片行", not any(p.startswith("![") for p in paras), paras)
check("去掉标题符号", not any(p.startswith("#") for p in paras), paras)

print("\n" + "=" * 50)
print("结果：%d 项检查，%d 项通过，%d 项失败" %
      (total[0], total[0] - len(fails), len(fails)))
if fails:
    print("失败项：", fails)
    sys.exit(1)
print("netutil.py 全部自测通过")
