---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '230d5c0a-48e9-4681-8559-e7a8a5a1576c'
  PropagateID: '230d5c0a-48e9-4681-8559-e7a8a5a1576c'
  ReservedCode1: '89063ca0-b99e-4a7a-b258-8798163e2507'
  ReservedCode2: '89063ca0-b99e-4a7a-b258-8798163e2507'
---

# 智学笔记 (ZhiXue Notes)

> 输入任意学习资源链接，一键生成重点笔记 + PDF + 思维导图

纯原生 HTML/CSS/JS 前端 + Python 轻量后端，零外部框架依赖，本地运行，隐私安全。

## 功能一览

- **多类型资源输入**：B站视频 / YouTube / 网盘分享文件夹 / PDF / Word / 网页文章 / 纯文本
- **字幕自动抓取**：B站 WBI API 字幕抓取 + YouTube yt-dlp 自动字幕
- **智能提炼**：按时间分段提取核心知识点，生成结构化知识图谱
- **三件套交付**：Markdown 笔记 + PDF（中文友好） + 思维导图 PNG
- **简洁大气 UI**：浅/深双主题、响应式布局、localStorage 本地持久化
- **隐私优先**：所有数据本地处理，不上传云端

## 快速开始

### 环境要求

- Python 3.8+（[下载地址](https://www.python.org/downloads/)）
- pip（Python 自带）

### 安装依赖

```bash
pip install requests reportlab matplotlib
# YouTube 字幕抓取可选安装
pip install yt-dlp
```

### 启动服务

```bash
# 1. 启动后端服务（端口 8766）
cd backend
python server.py

# 2. 用浏览器打开前端
#    方式一：直接双击 frontend/index.html
#    方式二：启动本地静态服务器
cd ../frontend
python -m http.server 8765
#    然后浏览器访问 http://localhost:8765
```

### 使用

1. 在主页输入框粘贴任意学习资源链接
2. 系统自动识别资源类型（B站视频 / 网盘 / 网页 / 文本等）
3. 点击「一键整理」
4. 等待流水线完成（字幕抓取 → 提炼重点 → 生成笔记 → 导出 PDF/思维导图）
5. 笔记卡片展开后可下载所有文件

## 项目结构

```
zhixue-notes/
├── frontend/
│   └── index.html          # 平台前端（纯原生 HTML/CSS/JS）
├── backend/
│   └── server.py           # Python 后端服务（REST API）
├── scripts/
│   ├── fetch_subtitles.py  # 多平台字幕抓取
│   ├── export_pdf.py       # Markdown → PDF 导出
│   ├── gen_mindmap.py      # 知识图谱 → 思维导图 PNG
│   └── screenshot_keyframes.py  # 视频关键帧截图
├── docs/
│   └── 上线可行性分析.md    # 上线方案分析（网站/小程序/GitHub）
├── README.md
├── LICENSE
└── .gitignore
```

## API 文档

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/process` | POST | 启动整理任务，传入 `{url, fmts}` |
| `/api/status?id=` | GET | 查询任务状态 |
| `/api/files/{id}/{filename}` | GET | 下载结果文件 |
| `/api/notes` | GET | 列出已完成笔记 |
| `/api/read?id=&file=` | GET | 读取文件内容 |

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | HTML5 + CSS3 + 原生 JavaScript（零框架依赖） |
| 后端 | Python 标准库 http.server + threading |
| PDF 导出 | ReportLab（中文字体自动探测） |
| 思维导图 | Matplotlib（横向树布局 + 按章节配色） |
| 字幕抓取 | requests + B站 WBI 签名 / yt-dlp |

## 与 TeleAgent 集成

本项目的核心能力源自 TeleAgent 技能 `video-note-master`（网课重点笔记大师）。通过 TeleAgent 触发该技能可获得完整的 AI 增强能力：

- 通俗解释（生活化类比 + 大白话）
- 多平台深度补充（中国大学MOOC / 可汗学院 / 3Blue1Brown 等真实链接）
- 自动截屏重点画面（Playwright 浏览器自动化）
- 定制化备考规划（艾宾浩斯复习时间表）

## License

MIT License - 详见 [LICENSE](LICENSE)

## 致谢

- 字幕抓取方案参考 B站 WBI 签名机制
- PDF 导出基于 ReportLab
- 思维导图基于 Matplotlib
- 设计灵感来自 Ai好记、BiliNote 等同类产品
