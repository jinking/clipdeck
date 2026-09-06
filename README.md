# Clipdeck

Clipdeck 是一个**通用网页与文档保存/归档工具**。它接收任意公开 URL、本地文件或粘贴文字，先可靠地拿回来、原样保存并建立版本（Layer 2），再编译成可重放、可追溯、可全文检索的 Markdown Evidence（Layer 3）。系统不做摘要、实体识别或事实抽取。

> 本项目由早期的领域专用采集器通用化而来：抓取、存储、安全、版本化与入库引擎本就与领域无关，现已去除全部领域硬编码，站点适配改为配置驱动。

支持的入口：

- 任意网页与微信公众号文章 URL（自动识别、自动编码检测）
- PDF、Word、文本、音频、视频文件上传
- PDF、Word、音频和视频公开直链
- 直接粘贴文字
- 批量 URL 导入与浏览器书签一键收藏（bookmarklet）

当前已实现网页、微信公众号与粘贴文本的本地 Markdown 入库，PDF/Word 的 MinerU 精准 API 解析，以及全文搜索、标签管理和批量导入。视频/播客保留转写 Provider 接口，本轮不执行转写。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/clipdeck
```

打开 <http://127.0.0.1:8765>。API 文档位于 <http://127.0.0.1:8765/docs>。

如果本机的 Clash/Surge 使用 `198.18.0.0/15` Fake-IP DNS，按下面方式启动；该开关只豁免这个代理专用网段，localhost 和真实内网地址仍然被拒绝：

```bash
CLIPDECK_ALLOW_PROXY_FAKE_IP=true .venv/bin/clipdeck
```

> 旧版 `SCI_*` 环境变量仍被兼容读取，无需修改现有 `.env`；新部署请使用 `CLIPDECK_*`。

浏览器动态采集是可选的重量依赖：

```bash
.venv/bin/pip install -e '.[browser]'
.venv/bin/crawl4ai-setup
.venv/bin/crawl4ai-doctor
```

没有安装 Crawl4AI 或本地浏览器运行时不可用时，普通网页会退化为原始 HTTP 归档，并在 RawAsset warnings 中明确记录；微信文章始终优先使用专项 Provider。

## 通用能力

- **编码自动检测**：抓取与入库均通过 `charset-normalizer` 识别 GBK/GB2312/UTF-8 等编码，中文老站点不再乱码。
- **全文搜索**：`GET /api/v1/search?q=` 直接扫描 Evidence 视图目录（正文不进 SQLite 的架构契约不变），子串匹配对中文天然友好，返回带上下文的摘要。
- **标签**：`PUT /api/v1/evidence/{id}/tags` 为任意归档打标签，支持按标签浏览与筛选。
- **批量导入**：`POST /api/v1/acquisitions/batch` 一次提交多个 URL，自动去重与拒绝非法项。
- **书签一键收藏**：把工作台里的「📌 存到 Clipdeck」拖到浏览器书签栏，在任意网页点击即经 `GET /api/v1/save?url=` 入队归档。
- **站点适配配置化**：平台显示名、学术标识符开关、自定义 LLM/OCR 提示词模板，全部在 `config/site_profiles.json` 中配置，无需改代码。

## 截断自动升级机制（可选）

部分站点（如知乎）对匿名请求返回**合法但被截断**的正文：页面能渲染、内容非空，服务端却把剩余部分锁在登录态之后，普通质量校验无法识别。Clipdeck 采用「配置驱动的截断检测 + 分级升级重取」：

1. 匿名抓取成功后，按 `config/site_profiles.json` 的 `truncation_markers`（域名→截断特征串，如知乎的 `"\"contentNeedTruncated\":true"`、`ContentItem-expandButton`）与内置通用短语判定是否被登录截断；
2. 命中后先将匿名版本原样归档（保留 provenance 证据链，warning 记 `login_truncated:<marker>`）；
3. **Tier 1 - 搜索引擎爬虫绕过**（默认开启 `CLIPDECK_SPIDER_BYPASS_ENABLED=true`）：使用百度蜘蛛 User-Agent 直接请求，许多平台对爬虫提供完整服务端渲染以保证 SEO 收录；若重取后不再截断，则归档为新版本（`provider_name=spider_bypass`）；
4. **Tier 2 - 本地已登录浏览器 CDP 重取**（可选 `CLIPDECK_LOGIN_BROWSER_ENABLED=true`）：若 Tier 1 未配置、失败或依然被截断，通过 CDP 连接本地已登录目标站点的浏览器重取；成功则作为新版本（`provider_name=login_browser`）入库；
5. 重取失败或依然被截断时，原匿名版本保持有效，并在任务 attempt 与 asset warning 中如实记录失败原因。

启用 Tier 2 CDP 时，需自行以调试端口启动浏览器（端点仅允许 loopback 回环地址，绝不关闭或改写你的浏览器会话）：

```bash
# 用独立 profile 启动一个已登录目标站点的 Chrome（首次需在该窗口登录一次）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir=/tmp/clipdeck-login-profile
# 在 .env 中：
#   CLIPDECK_LOGIN_BROWSER_ENABLED=true
#   CLIPDECK_CDP_URL=http://127.0.0.1:9222
```

## MinerU 配置

真实 Token 只能写入本地 `.env` 或启动进程环境，不能写入 Git、SQLite、日志或技术文档：

```bash
cp .env.example .env
# 在 .env 中填写 MINERU_API_TOKEN；.env 已被 .gitignore 排除。
set -a
source .env
set +a
.venv/bin/clipdeck
```

PDF/Word 原始文件不会自动外发。先完成采集，再在页面点击“解析入库”并确认，系统才会把该文档上传给 MinerU。默认模型是 `vlm`；API 也允许白名单切换到 `pipeline`。

网页正文的 LLM 提取同样默认关闭。若确实允许把网页正文发送到外部 LLM，必须同时配置专用的 `LLM_API_KEY` 并设置 `CLIPDECK_LLM_EXTERNAL_PROCESSING_ALLOWED=true`。系统不会读取或复用 `OPENAI_API_KEY`；`LLM_BASE_URL` 必须是无内嵌凭据的 HTTPS 地址。未显式授权时始终使用本地启发式提取。

## 数据保存

大文件和正文不进入 SQLite。数据库只保存任务、版本、状态、SHA-256、MIME、来源、外部任务 ID、Blob 引用和标签。原始及派生 bytes 保存在 `data/blobs/sha256/`，并生成两类只读可读视图：

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
POST /api/v1/acquisitions/batch
POST /api/v1/acquisitions/text
POST /api/v1/acquisitions/file
GET  /api/v1/save?url=            # 书签一键收藏
POST /api/v1/ingestions
GET  /api/v1/ingestions/{run_id}
GET  /api/v1/evidence
GET  /api/v1/search?q=&tag=       # 全文搜索 / 标签浏览
GET  /api/v1/tags
PUT  /api/v1/evidence/{id}/tags
GET  /api/v1/evidence/{evidence_id}/content
GET  /api/v1/evidence/{evidence_id}/meta
GET  /api/v1/evidence/{evidence_id}/package
```

Layer 3 使用 SQLite 持久化状态和一个进程内单 Worker 串行执行；已保存 `batch_id` 的 MinerU 任务在重启时继续轮询，已保存结果 ZIP 的任务可离线继续组装。

## 测试

```bash
.venv/bin/pytest --cov=clipdeck.acquisition --cov=clipdeck.ingestion --cov-report=term-missing
```

系统设计详见 [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) 与 [Layer 3 + MinerU 技术规格](docs/06-SCI-Radar-Layer3-Ingestion-MinerU整合技术设计与开发规格-V1.1.md)。
