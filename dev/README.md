# dev/ — 回归测试

本目录是智学笔记的**回归测试套件**。改动后端代码后跑一遍，能在几秒内发现破坏性修改。

## 快速开始

```bash
# 跑全部（185 项检查，约 1~2 分钟）
python dev/run_tests.py

# 只跑其中几组
python dev/run_tests.py config netutil
python dev/run_tests.py e2e
```

退出码 `0` = 全部通过，`1` = 有失败（便于接入 CI）。

## 测试套件

| 关键字 | 文件 | 项数 | 覆盖内容 |
|---|---|---|---|
| `config` | `test_config.py` | 23 | 配置读写、环境变量优先级、Key 打码、边界值夹取、损坏文件容错、深拷贝防污染 |
| `netutil` | `test_netutil.py` | 40 | B站短链跳转、av→BV 换算、`?p=N` 多P、越界夹取、标题清洗、YouTube 解析、失败分支可读性 |
| `docextract` | `test_docextract.py` | 23 | 手工构造 PDF/DOCX 字节流解析、多编码试探、BOM 处理、段落切分 |
| `llm` | `test_llm.py` | 31 | 分块（长文稿不被截断）、章节规划、JSON 容错、并发、降级、连接失败不抛异常 |
| `server` | `test_server.py` | 47 | 全部 HTTP 接口、端到端整理、路径穿越防护、AI 配置接口、任务清理、笔记历史重建 |
| `e2e` | `test_e2e_llm.py` | 21 | 接入大模型 → 整理讲义 → 笔记落盘（验证标题/通俗解释/必背考点真的写进文件） |

## 设计原则

1. **不联网**。大模型用本地 mock 的 OpenAI 兼容服务（`ThreadingHTTPServer`）替代；
   链接解析用 monkeypatch 顶掉网络请求，只测逻辑。
2. **不污染用户数据**。所有测试把数据写到 `dev/_work/`（已 gitignore），
   不会碰你真实的 `~/.zhixue-notes/`。
3. **能跑在只读源码目录**。工作目录可用环境变量覆盖：

   ```bash
   ZHIXUE_TEST_WORK=/tmp/zhixue-test python dev/run_tests.py
   ```

4. **断言可读**。每个断言都带名字与失败时的实际值，例如：

   ```text
   PASS  短链解析出 bvid
   FAIL  带 time 轴  -> [('学习率', None), ('动量法', None)]
   ```

## 加新测试

在 `dev/` 下新建 `test_xxx.py`，并：

1. 头部按现有文件的写法推导路径：

   ```python
   HERE = os.path.dirname(os.path.abspath(__file__))
   REPO = os.path.dirname(HERE)
   BACKEND = os.path.join(REPO, "backend")
   WORK = os.environ.get("ZHIXUE_TEST_WORK") or os.path.join(HERE, "_work")
   os.makedirs(WORK, exist_ok=True)
   sys.path.insert(0, BACKEND)
   ```

2. 用统一的 `check(name, cond, extra)` 打印 `PASS`/`FAIL`
   （`run_tests.py` 靠这两个词统计结果）。
3. 结尾打印一行 `结果：N 项检查，M 项通过，K 项失败`，失败时 `sys.exit(1)`。
4. 把新文件登记到 `run_tests.py` 的 `SUITES` 列表里。

## 这套测试是怎么来的

它们是在一次集中优化中写下的，用来验证并锁住以下修复（都曾真实出过问题）：

- `faster_whisper_available()` / `transcribe_video()` 曾被**重复定义两次**
- 配置合并曾是**浅拷贝**，调用方一改返回值就**永久污染模块默认值**
- 时间轴判定曾写成 `any(s for s, _ in items)`，导致**首条字幕为 0 秒时丢失时间标签**
- 短链 `b23.tv` 曾**完全不处理**（链接里没有 BV 号，必须跟随跳转）
- 多 P 视频的 `--cid` 参数曾**声明了却从不传**，永远只抓第一 P
- 知识点标题曾是「句子截前 36 字」，所以笔记像**半截话**
- 大模型只吃文稿**前 3000/4000 字**，长视频后半段完全没参与提炼

保留这些测试，是为了这些问题不再回来。
