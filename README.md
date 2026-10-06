---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '6e1ad21a-22c9-42b8-bc35-49897ac14a93'
  PropagateID: '6e1ad21a-22c9-42b8-bc35-49897ac14a93'
  ReservedCode1: '16b50f7d-20b1-41e4-8f57-6001ccfce7a8'
  ReservedCode2: '16b50f7d-20b1-41e4-8f57-6001ccfce7a8'
---

# 智学笔记 (ZhiXue Notes)

<div align="center">

**输入任意学习资源链接，一键生成重点笔记 + PDF + 思维导图**

本地运行 · 隐私安全 · AI 增强 · 零框架依赖

[![stars](https://img.shields.io/github/stars/Yan120307/zhixue-notes.svg?style=flat-square&color=gold)](https://github.com/Yan120307/zhixue-notes/stargazers)
[![forks](https://img.shields.io/github/forks/Yan120307/zhixue-notes.svg?style=flat-square&color=blue)](https://github.com/Yan120307/zhixue-notes/network)
[![license](https://img.shields.io/github/license/Yan120307/zhixue-notes.svg?style=flat-square&color=green)](./LICENSE)
[![python](https://img.shields.io/badge/Python-3.8+-blue.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![html](https://img.shields.io/badge/HTML5-CSS3-JS-orange.svg?style=flat-square&logo=html5&logoColor=white)](https://developer.mozilla.org/)
[![platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg?style=flat-square)]()
[![release](https://img.shields.io/badge/Release-v1.1-blueviolet.svg?style=flat-square)]()
[![issues](https://img.shields.io/github/issues/Yan120307/zhixue-notes.svg?style=flat-square&color=orange)](https://github.com/Yan120307/zhixue-notes/issues)

**[功能特性](#-功能特性) · [效果展示](#-效果展示) · [快速开始](#-快速开始) · [配置大模型](#-配置大模型可选) · [API 文档](#-api-文档) · [路线图](#-路线图)**

</div>

---

## ✨ 功能特性

### 核心能力
- **多类型资源输入** — B站视频 / YouTube / 网盘分享文件夹 / PDF / Word / 网页文章 / 纯文本，粘贴即自动识别
- **字幕自动抓取** — B站 WBI API 字幕抓取 + YouTube yt-dlp 自动字幕，无需手动下载
- **智能提炼** — 按时间分段提取核心知识点，生成结构化知识图谱（JSON）
- **AI 通俗解释**（可选） — 接入通义千问 / OpenAI，每个知识点生成「一句话掌握」+「生活化通俗解释」
- **AI 考点提炼**（可选） — 自动生成 5-8 个必背考点清单

### 交付格式（三件套）
| 格式 | 说明 |
|---|---|
| **Markdown** | 结构化笔记源文件 |
| **PDF** | 中文友好排版，内嵌截图与表格 |
| **思维导图 PNG** | 横向树布局，按章节自动配色 |

### 平台特色
- **简洁大气 UI** — 浅/深双主题、响应式布局、荧光笔签名设计
- **笔记在线预览** — 点击即在弹窗中阅读完整笔记
- **历史搜索** — 按标题/来源/类型即时筛选
- **本地优先** — localStorage 持久化 + 本地 Python 后端，数据不出本机

## 📸 效果展示

| 主页输入 | 整理流水线 |
|:---:|:---:|
| ![主页](docs/images/screenshot-home.png) | ![流水线](docs/images/screenshot-pipeline.png) |

| 资源识别 | 笔记交付 |
|:---:|:---:|
| ![输入](docs/images/screenshot-input.png) | ![结果](docs/images/screenshot-result.png) |

> 更多截图见 [docs/images/](docs/images/)

## 🚀 快速开始

### 环境要求

- Python 3.8+
- 现代浏览器（Chrome / Edge / Firefox）

### 一分钟启动

```bash
# 1. 克隆仓库
git clone https://github.com/Yan120307/zhixue-notes.git
cd zhixue-notes

# 2. 安装依赖
pip install requests reportlab matplotlib

# 3. 启动后端服务
cd backend && python server.py

# 4. 另开终端，启动前端
cd frontend && python -m http.server 8765
# 浏览器打开 http://localhost:8765
```

> 也可以直接双击 `frontend/index.html` 打开（无需启动前端服务器，但需保持后端运行）

### 使用

1. 在主页输入框粘贴任意学习资源链接
2. 系统自动识别资源类型
3. 点击「**一键整理**」
4. 等待流水线完成，笔记卡片展开后即可下载所有文件

## 🤖 配置大模型（可选）

配置后可获得 AI 通俗解释 + 考点提炼。未配置时自动降级为基础提炼，不影响使用。

### 方式一：通义千问（推荐国内用户）

```bash
# 获取 API Key: https://bailian.console.aliyun.com/ → API-KEY 管理 → 创建
# Windows PowerShell
$env:DASHSCOPE_API_KEY = "sk-你的Key"

# Linux/macOS
export DASHSCOPE_API_KEY="sk-你的Key"
```

### 方式二：OpenAI

```bash
export OPENAI_API_KEY="sk-你的Key"
# 可选：切换模型
export ZHIXUE_LLM_MODEL="gpt-4o-mini"
```

配置后重启 `server.py`，整理时流水线会显示「大模型增强中」，笔记将包含：
- **一句话掌握**：用一句大白话概括每个知识点
- **通俗解释**：生活化类比 + 零基础友好
- **必背考点**：AI 生成的考试重点清单

## 📖 API 文档

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/process` | POST | 启动整理任务，body: `{"url": "...", "fmts": ["md","pdf","mindmap"]}` |
| `/api/status?id=` | GET | 轮询任务状态与进度日志 |
| `/api/files/{id}/{filename}` | GET | 下载结果文件（md/pdf/png/json） |
| `/api/notes` | GET | 列出所有已完成笔记 |
| `/api/read?id=&file=notes.md` | GET | 读取笔记内容（在线预览用） |

<details>
<summary>展开查看 API 调用示例</summary>

```bash
# 启动整理任务
curl -X POST http://localhost:8766/api/process \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.bilibili.com/video/BV1xx411c7mD", "fmts": ["md", "pdf", "mindmap"]}'

# 返回 {"task_id": "tabc123...", "type": {...}}

# 查询状态
curl http://localhost:8766/api/status?id=tabc123

# 下载 PDF
curl -o 笔记.pdf http://localhost:8766/api/files/tabc123/notes.pdf
```

</details>

## 🏗️ 项目结构

```
zhixue-notes/
├── frontend/
│   └── index.html              # 平台前端（纯原生 HTML/CSS/JS，零依赖）
├── backend/
│   ├── server.py               # Python REST API（字幕抓取+提炼+导出）
│   └── llm.py                  # 大模型模块（通义千问/OpenAI 兼容）
├── scripts/
│   ├── fetch_subtitles.py      # 多平台字幕抓取（B站 WBI / yt-dlp）
│   ├── export_pdf.py           # Markdown → PDF（reportlab，中文友好）
│   ├── gen_mindmap.py          # 知识图谱 → 思维导图 PNG（matplotlib）
│   └── screenshot_keyframes.py # 视频关键帧截图（playwright）
├── docs/
│   ├── images/                 # README 截图
│   └── 上线可行性分析.md        # 上线方案分析（网站/小程序/GitHub）
├── README.md
├── LICENSE
└── .gitignore
```

## 🛠️ 技术栈

| 层 | 技术 |
|---|---|
| 前端 | HTML5 + CSS3 + 原生 JS（零框架、零依赖、单文件） |
| 后端 | Python 标准库 `http.server` + `threading` |
| LLM | 通义千问 DashScope / OpenAI 兼容接口（`urllib`，无需 SDK） |
| PDF | ReportLab（中文字体自动探测：微软雅黑→黑体→宋体→CID） |
| 思维导图 | Matplotlib（横向树 + 贝塞尔连线 + 黄金角配色） |
| 字幕 | requests + B站 WBI 签名 / yt-dlp |

## 🗺️ 路线图

- [x] 多类型资源输入（视频/网盘/文档/网页/文本）
- [x] 字幕自动抓取（B站/YouTube）
- [x] 基础提炼（分章节 + 关键句）
- [x] 三件套导出（Markdown + PDF + 思维导图）
- [x] 大模型增强（通义千问/OpenAI 通俗解释 + 考点）
- [x] 笔记在线预览 + 历史搜索
- [x] 浅/深双主题
- [ ] Docker 一键部署
- [ ] 视频关键帧自动截图（Playwright 集成）
- [ ] 网盘文件夹批量浏览
- [ ] Anki 卡片导出
- [ ] Obsidian Vault 直接写入
- [ ] 浏览器插件（一键收藏当前页面）

## 🤝 贡献指南

欢迎 Issue / PR！

1. Fork 本仓库
2. 创建分支：`git checkout -b feature/amazing`
3. 提交更改：`git commit -m "Add amazing feature"`
4. 推送分支：`git push origin feature/amazing`
5. 提交 Pull Request

## 📄 License

[MIT](./LICENSE) © 2026 智学笔记

## 🙏 致谢

- 字幕抓取参考 [BiliNote](https://github.com/JefferyHcool/BiliNote) 的 WBI 签名方案
- PDF 导出基于 [ReportLab](https://www.reportlab.com/)
- 思维导图基于 [Matplotlib](https://matplotlib.org/)
- 设计灵感来自 Ai好记、BiliNote、NoteGPT 等产品

---

<div align="center">

如果这个项目对你有帮助，欢迎点一个 **Star** ⭐ 让更多人看到！

**[⭐ Star](https://github.com/Yan120307/zhixue-notes/stargazers) · [🍴 Fork](https://github.com/Yan120307/zhixue-notes/fork) · [📢 Issue](https://github.com/Yan120307/zhixue-notes/issues)**

</div>

> AI生成