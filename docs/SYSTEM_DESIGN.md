# SCI Acquisition 下载系统设计 V1.1

## 1. 产品定位

SCI Acquisition 是一个单一职责应用：接收公开 URL、本地文件或粘贴文字，把输入完整、可追溯、可版本化地保存为 `RawAsset`。

它的终点是 RawAsset，不是“可阅读文章”。下列能力明确属于后续 Ingestion：

- HTML 正文、标题、作者、时间提取
- PDF/Word 的文字、表格、图片和 OCR 提取
- 视频/播客的音轨抽取、语音识别、说话人分离与时间戳
- 文本清理、标准化、分段和去重
- 实体、Claim、SCI 相关性与证据判断

这样做让任何解析或转写算法升级都能直接重跑已经保存的原始内容，不必重新访问来源。

## 2. 统一入口与产物

```text
URL ─────────┐
文件上传 ────┼─> AcquisitionTask -> FetchAttempt -> RawAsset -> Outbox Event
粘贴文本 ────┘                                │
                                              └-> content-addressed BlobStore
```

三种入口只在“如何获得 bytes”上不同。获得以后统一使用：

- `resource_key`：来源身份的稳定哈希
- `version_no`：同一来源的追加式版本号
- `raw_sha256`：原始主 Blob 的内容指纹
- `previous_asset_id` / `changed_from_previous`：版本链
- `provider_meta.ingestion_hint`：交给下一层的处理建议
- `task_id -> attempt_id -> asset_id -> blob_id`：完整追溯链

## 3. 路由矩阵

| 输入 | ResourceType | Acquisition Provider | 原始主资产 | Ingestion hint |
|---|---|---|---|---|
| 普通网页 URL | `web_page` | Crawl4AI | Rendered HTML/MHTML | `html_ingestion` |
| 微信文章 URL | `wechat_article` | WechatArticleProvider | HTTP response bytes | `html_ingestion` |
| PDF URL/上传 | `pdf` | DirectDownload/LocalInput | 原始 PDF | `document_text_extraction` |
| Word URL/上传 | `word` | DirectDownload/LocalInput | 原始 DOC/DOCX | `document_text_extraction` |
| 粘贴文本/文本文件 | `text` | LocalInput | UTF-8 原文/原始文件 | `text_normalization` |
| 视频 URL/上传 | `video` | DirectDownload/LocalInput | 原始视频文件 | `media_transcription` |
| 播客 URL/上传 | `podcast` | DirectDownload/LocalInput | 原始音频文件 | `media_transcription` |

对媒体 URL，V1 仅支持可直接下载的公开音视频直链。YouTube、Bilibili、小宇宙、Apple Podcasts 等平台页面未来应新增独立 Provider，不能把平台解析逻辑塞进通用 HTTP Provider。

## 4. PDF、Word 与粘贴文字如何统一入库

### 4.1 文件上传

上传请求到达后立即写入 BlobStore，然后才创建/执行任务。任务失败不会导致文件丢失。原文件不转格式、不改名、不覆盖；数据库只保存 Blob 引用和技术元数据。

同一逻辑适用于 PDF、DOC/DOCX、TXT、音频、视频和未知二进制文件。MIME 与扩展名只参与技术路由，不作为内容真实性判断。

### 4.2 粘贴文字

粘贴文字按 UTF-8 编码原样保存为 `pasted_text` Blob。Acquisition 不进行空白压缩、Markdown 转换或语言识别。若用户希望把同一份长期笔记持续更新，可传稳定 `source_key`，每次提交形成新版本；不传时每次粘贴视为独立来源。

### 4.3 下游交接

Outbox 产生 `raw_asset.created` 事件。未来 Ingestion Worker 订阅事件，按 `ingestion_hint` 选择 Parser/Transcriber，输出独立的 `RawEvidencePackage`。派生产物必须引用 `asset_id` 和输入 Blob SHA-256，不能回写或覆盖 RawAsset。

## 5. 视频与播客预留设计

未来媒体链路：

```text
Media RawAsset
  -> MediaProbe（时长/编码/音轨，仅技术元数据）
  -> AudioExtraction（派生 Blob）
  -> TranscriptionJob
  -> TranscriptArtifact（段落 + 时间戳 + speaker，可选）
  -> Ingestion Normalizer
  -> RawEvidencePackage
```

需要新增但不破坏当前接口的对象：

- `MediaPlatformProvider`：从平台页面解析并下载合法公开媒体
- `IngestionJob`：与 AcquisitionTask 分表，重试互不影响
- `DerivedBlobRef`：记录父 Blob、工具版本和参数
- `TranscriptArtifact`：记录语言、模型、时间戳、说话人和置信度
- `TranscriptionProvider`：本地 Whisper、云 ASR 等可替换适配器

关键约束：下载成功不依赖转写成功；同一个原媒体可用不同模型多次转写；转写结果不是 RawAsset，也不能改变下载任务的成功状态。

## 6. 持久化与一致性

BlobStore 使用 SHA-256 内容寻址：`data/blobs/sha256/ab/cd/<hash>.blob`。相同 bytes 只保存一次。SQLite 保存 Task、Attempt、RawAsset 和 Outbox；写入 RawAsset 与 Outbox 事件位于同一数据库事务语义中。

每个 RawAsset 另有 `data/raw-assets/<日期>/<asset-id>/` 可读视图，包含 `manifest.json`、带正常扩展名的 `original.*`、`images/` 和 `attachments/`。实体文件通过硬链接指向 BlobStore，因此双视图不会复制内容；可读视图损坏时可完全由数据库与 BlobStore 重建。

当前本地版使用 SQLite 单进程队列。生产演进：

1. PostgreSQL + `FOR UPDATE SKIP LOCKED` 支持多 Worker。
2. BlobStore 替换为 S3/MinIO/OSS，业务模型不感知路径。
3. Outbox Relay 把 `raw_asset.created` 投递到消息队列。
4. 任务量进一步增加时接入 Redis/RabbitMQ/Kafka；Task/Attempt/Asset 契约保持不变。

## 7. 安全与资源控制

- URL 只允许 HTTP(S)，拒绝内嵌账号密码。
- 发起请求前解析 DNS，拒绝 loopback、内网、链路本地和保留地址。
- 每次重定向重新执行 SSRF 校验，最多 6 跳。
- 流式下载并限制响应大小；上传大小由 `SCI_MAX_UPLOAD_BYTES` 控制。
- 不绕过登录、验证码、付费墙或访问控制。
- Blob 下载 API 当前定位为本地管理用途，生产部署必须加认证与授权。

## 8. 失败与版本语义

每次真实网络请求形成 FetchAttempt。HTTP 传输成功与资源校验成功分开记录。可重试错误按有上限的指数退避执行；错误页的原始响应仍作为 debug Blob 保存。

重新抓取永远创建新的 Task 和 RawAsset 版本，不覆盖历史。即使新旧 SHA-256 相同，也保留新的版本和采集时间，并把 `changed_from_previous` 标为 `false`。

## 9. API

- `POST /api/v1/acquisitions`：提交 URL
- `POST /api/v1/acquisitions/file`：上传文件
- `POST /api/v1/acquisitions/text`：粘贴文字
- `GET /api/v1/acquisitions`：任务列表
- `GET /api/v1/acquisitions/{id}`：任务及全部 Attempts
- `GET /api/v1/raw-assets` / `{id}`：资产清单
- `GET /api/v1/blobs/{blob_id}`：下载原始 Blob（管理接口）
- `POST /api/v1/raw-assets/{id}/refetch`：追加式重新采集
- `GET /api/v1/dashboard/summary`：工作台摘要

## 10. 实施路线

### 已实现的本地首版

- 统一 Source/Resource/Provider/RawAsset 模型
- 文件和文本 Raw First
- 内容寻址 BlobStore、SQLite 账本、版本链与 Outbox
- URL 分类、微信专项 Provider、媒体/文档直链 Provider
- Crawl4AI 适配口与未安装时的显式 HTTP fallback
- SSRF、重定向与大小限制
- API、操作工作台与自动化测试

### 生产化下一步

1. 安装并固定经验证的 Crawl4AI runtime，补全 MHTML 采集的真实环境测试。
2. 把上传改为流式落盘，支持超大媒体和断点续传。
3. PostgreSQL Repository、独立 Worker 与任务租约。
4. 用户认证、Blob 权限、审计日志与域级限速。
5. 新建独立 Ingestion 服务，先实现 PDF/Word Parser，再接媒体转写。
6. 为具体媒体平台按合规范围新增 Provider。
