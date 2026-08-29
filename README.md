# SCI Acquisition

SCI Radar 的采集下载与统一入库应用。Layer 2 负责把资源可靠地拿回来、原样保存并建立版本；Layer 3 把 RawAsset 编译成可重放、可追溯的 Markdown Evidence。系统不做摘要、实体识别、Fact 抽取或音视频转写。

支持的入口：

- 普通网页与微信公众号文章 URL
- PDF、Word、文本、音频、视频文件上传
- PDF、Word、音频和视频公开直链
- 直接粘贴文字

当前已实现网页、微信公众号与粘贴文本的本地 Markdown 入库，以及 PDF/Word 的 MinerU 精准 API 解析。视频/播客保留转写 Provider 接口，本轮不执行转写。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/sci-acquisition
```

打开 <http://127.0.0.1:8765>。API 文档位于 <http://127.0.0.1:8765/docs>。

如果本机的 Clash/Surge 使用 `198.18.0.0/15` Fake-IP DNS，按下面方式启动；该开关只豁免这个代理专用网段，localhost 和真实内网地址仍然被拒绝：

```bash
SCI_ALLOW_PROXY_FAKE_IP=true .venv/bin/sci-acquisition
```

浏览器动态采集是可选的重量依赖：

```bash
.venv/bin/pip install -e '.[browser]'
.venv/bin/crawl4ai-setup
.venv/bin/crawl4ai-doctor
```

没有安装 Crawl4AI 或本地浏览器运行时不可用时，普通网页会退化为原始 HTTP 归档，并在 RawAsset warnings 中明确记录；微信文章始终优先使用专项 Provider。

## MinerU 配置

真实 Token 只能写入本地 `.env` 或启动进程环境，不能写入 Git、SQLite、日志或技术文档：

```bash
cp .env.example .env
# 在 .env 中填写 MINERU_API_TOKEN；.env 已被 .gitignore 排除。
set -a
source .env
set +a
.venv/bin/sci-acquisition
```

PDF/Word 原始文件不会自动外发。先完成采集，再在页面点击“解析入库”并确认，系统才会把该文档上传给 MinerU。默认模型是 `vlm`；API 也允许白名单切换到 `pipeline`。

网页正文的 LLM 提取同样默认关闭。若确实允许把网页正文发送到外部 LLM，必须同时配置专用的 `LLM_API_KEY` 并设置 `SCI_LLM_EXTERNAL_PROCESSING_ALLOWED=true`。系统不会读取或复用 `OPENAI_API_KEY`；`LLM_BASE_URL` 必须是无内嵌凭据的 HTTPS 地址。未显式授权时始终使用本地启发式提取。

## 数据保存

大文件和正文不进入 SQLite。数据库只保存任务、版本、状态、SHA-256、MIME、来源、外部任务 ID 和 Blob 引用。原始及派生 bytes 保存在 `data/blobs/sha256/`，并生成两类只读可读视图：

```text
manifest.json
original.html / original.pdf / original.docx / ...
images/image-001.jpg
attachments/...

data/evidence/<日期>/<evidence-id>/
content.md
meta.yaml
assets/...
diagnostics/...
```

可读文件是指向 BlobStore 的硬链接，与底层 Blob 使用同一个 inode，不复制文件内容，也不额外占用一份磁盘空间。MinerU 原始结果 ZIP 永久保存在 BlobStore，但默认下载 Evidence Package 时不会把诊断 ZIP 再打包进去。

## API

主要入口：

```text
POST /api/v1/acquisitions
POST /api/v1/acquisitions/text
POST /api/v1/acquisitions/file
POST /api/v1/ingestions
GET  /api/v1/ingestions/{run_id}
GET  /api/v1/evidence
GET  /api/v1/evidence/{evidence_id}/content
GET  /api/v1/evidence/{evidence_id}/meta
GET  /api/v1/evidence/{evidence_id}/package
```

Layer 3 使用 SQLite 持久化状态和一个进程内单 Worker 串行执行；已保存 `batch_id` 的 MinerU 任务在重启时继续轮询，已保存结果 ZIP 的任务可离线继续组装。

## 测试

```bash
.venv/bin/pytest --cov=sci_radar.acquisition --cov=sci_radar.ingestion --cov-report=term-missing
```

系统设计详见 [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) 与 [Layer 3 + MinerU 技术规格](docs/06-SCI-Radar-Layer3-Ingestion-MinerU整合技术设计与开发规格-V1.1.md)。
