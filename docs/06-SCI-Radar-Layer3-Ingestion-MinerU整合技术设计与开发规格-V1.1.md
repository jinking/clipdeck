# SCI Radar Layer 3：Ingestion + MinerU 整合技术设计与开发规格

> 版本：V1.1 Approved｜日期：2026-08-28｜状态：Implementation Authorized
> 2026-08-28 已确认：SQLite、单 Worker、补齐 Crawl4AI；其余采用本文建议项。Token 仅通过本地环境变量或 Secret Manager 注入，不写入本文。

## 1. 目标与系统边界

目标：把 Layer 2 的不可变 `RawAsset` 编译成可重放、可追溯、AI 易读的 Evidence Package；PDF 和 Word 统一通过 MinerU 精准解析 API 转为 Markdown。

```text
Discovery → Acquisition → RawAsset → Ingestion → Evidence Package → Knowledge Processing
```

Layer 3 负责正文编译、基础 Metadata、Identifier、图片索引和质量标记；不覆盖 RawAsset，不重新访问原网页，不生成 Entity、Fact、Event、Timeline 或患者价值判断。

数据库只保存任务、版本、状态、外部任务 ID 和 Blob 引用。HTML、PDF、Word、Markdown、YAML、ZIP 和图片实体均不进入数据库。

## 2. V1.1 Scope

| ResourceType | Converter | 行为 |
|---|---|---|
| `WECHAT_ARTICLE` | WechatArticleConverter | 本地 HTML → Markdown，映射 Child Images |
| `WEB_PAGE` | WebPageConverter | 本地 Rendered/Primary HTML → Markdown；MHTML 恢复资源 |
| `TEXT` | TextConverter | UTF-8 原文 → Markdown，不改写语义 |
| `PDF` | MinerUDocumentConverter | Blob → MinerU → Result ZIP → Markdown + assets |
| `WORD` | MinerUDocumentConverter | DOC/DOCX Blob → MinerU → Result ZIP → Markdown + assets |
| `VIDEO/PODCAST` | 预留 | 后续带时间戳转写稿 |

必须实现：独立 IngestionRun、持久化 Worker、Evidence 双视图、Pipeline Fingerprint、MinerU 任务恢复、Result ZIP 保存、Metadata、Identifier、Partial Success、API/UI、可观测性和测试。

V1.1 不实现：实际媒体转写、复杂 AST、自动拆分超限文档、私有化 MinerU、Knowledge 对象。

## 3. 架构决策

1. **RawAsset 是唯一原始证据**：Layer 3 全部产物可删除重建。
2. **统一输出**：`content.md + meta.yaml + assets/`。
3. **PDF/Word 使用 Token 鉴权的 MinerU 精准 API**：不以 10MB/20 页的 Agent 轻量 API 作为生产主链路。
4. **本地 Blob 使用预签名上传**：不暴露内部 Blob URL。
5. **外部处理必须授权**：`local_only=true` 或 `external_processing_allowed=false` 时不得上传 MinerU。
6. **派生内容有明确标记**：机器视觉观察不能伪装成原文。
7. **双视图不复制**：Evidence 文件写 BlobStore，可读目录使用只读硬链接。
8. **MinerU 可替换**：业务层只依赖 `DocumentParseProvider`，不直接依赖远端 JSON。

## 4. 总体架构

```text
raw_asset.created
      ↓
Ingestion Worker → IngestionService → ConverterResolver
                                      ├─ WeChat / Web / Text（本地）
                                      └─ MinerU Document（PDF / Word）
                                                    ↓
                                      ConversionResult
                                                    ↓
                         Metadata + Identifier + Image Pipeline
                                                    ↓
                         EvidenceAssembler + Quality Validator
                                                    ↓
                BlobStore + Evidence View + DB + Outbox Event
```

## 5. 核心模型

### 5.1 IngestionRun

```python
class IngestionRun(BaseModel):
    run_id: UUID
    asset_id: UUID
    resource_type: ResourceType
    pipeline_version: str
    pipeline_fingerprint: str
    converter_name: str
    converter_version: str
    status: IngestionStatus
    attempt_count: int
    evidence_id: str | None
    warnings: list[str]
    last_error_code: str | None
    last_error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
```

状态：`pending → preparing → uploading → submitted → polling → downloading → assembling → success/partial/failed/quarantined`。

幂等唯一键：`(asset_id, pipeline_fingerprint)`。

### 5.2 ExternalParseJob

保存 `run_id/provider/api_version/batch_id/data_id/trace_id/remote_state/request_options/poll_count/result_archive_blob_id/时间/错误`。禁止保存 Token、Authorization、完整预签名 URL、Result URL 签名 Query 和 Callback seed。

### 5.3 EvidenceDocument

保存 `evidence_id/run_id/asset_id/raw_resource_key/raw_version_no/raw_sha256/evidence_version_no/pipeline_fingerprint/markdown_blob_id/yaml_blob_id/source_archive_blob_id/status/warnings/created_at`。

### 5.4 DerivedArtifact

角色：`EVIDENCE_MARKDOWN`、`EVIDENCE_YAML`、`MINERU_RESULT_ARCHIVE`、`MINERU_CONTENT_LIST`、`MINERU_MIDDLE_JSON`、`MINERU_MODEL_JSON`、`DERIVED_IMAGE`、`VISION_ANALYSIS_JSON`。

每项记录 `blob_id/parent_blob_id/sha256/mime_type/size/logical_path/producer/version`。

## 6. Evidence Package

```text
data/evidence/2026/08/28/<evidence_id>/
├── content.md
├── meta.yaml
├── assets/
│   ├── img-001.jpg
│   └── img-002.png
└── diagnostics/
    ├── mineru-result.zip
    └── content_list.json
```

所有实体是 BlobStore 的只读硬链接；删除视图不影响 Canonical Blob，可由数据库重建。

`content.md` 保留标题层级、段落、列表、引用、表格、公式、链接、图片位置与 Caption。PDF 能可靠定位时加入：

```markdown
<!-- source-page: 12 -->
```

视觉派生信息格式：

```markdown
<!-- derived:image-analysis image_id=img-003 analysis_version=vision-v1 -->
> **机器视觉提取，不属于原文**
> - 直接可见信息：年龄范围 18–65 岁
> - 不确定项：机构名称末尾字符不清晰
> - 置信度：0.91
```

`meta.yaml` Schema 2 至少包含：source、time、document、identifiers、raw、conversion、artifacts、images、quality。不得包含正文、Token、签名 URL、长 OCR、Fact 或 Claim 结论。

## 7. MinerU 精准 API 集成

### 7.1 选择依据

根据 [MinerU 官方 API 文档](https://mineru.net/apiManage/docs)，精准 API 支持 PDF、DOC、DOCX 等，单文件最大 200MB、最多 200 页，异步返回含 Markdown、JSON 和图片的 ZIP；官方当前说明每日前 1000 页享有最高优先级，限制和配额实施时必须再次核对。

Agent 轻量 API 仅支持 10MB/20 页且只返回 Markdown CDN 链接，不用于正式主链路。

### 7.2 本地文件工作流

```text
POST /api/v4/file-urls/batch（files 数组只放 1 个也可）
  → 获得 batch_id + file_urls
  → 从 BlobStore 流式 PUT 到预签名 URL（不设置 Content-Type）
  → MinerU 自动提交解析
  → GET /api/v4/extract-results/batch/{batch_id}
  → done 后下载 full_zip_url
```

上传地址按官方文档有效 24 小时，一次最多申请 50 个。本系统单资产默认申请一个地址。

申请示例：

```json
{
  "files": [{
    "name": "asset-<asset_id>.pdf",
    "data_id": "<asset_uuid>-<fingerprint_prefix>",
    "is_ocr": false
  }],
  "model_version": "vlm",
  "enable_formula": true,
  "enable_table": true,
  "language": "ch"
}
```

`data_id` 只用字母、数字、下划线、短划线和句点，≤128 字符。

### 7.3 参数策略

默认 `model_version=vlm`、`enable_formula=true`、`enable_table=true`、`language=ch`、`is_ocr=false`。允许配置切换 `pipeline`；`MinerU-HTML` 不用于 PDF/Word。

PDF 首次不强制 OCR；若 `MARKDOWN_EMPTY/CONTENT_TOO_SHORT` 且疑似扫描件，以 `is_ocr=true` 和新 Fingerprint 最多重放一次。Word 不做 OCR fallback。

### 7.4 远端状态映射

| MinerU | 本地 | 行为 |
|---|---|---|
| `waiting-file` | uploading/submitted | 检查上传，继续轮询 |
| `pending` | submitted | 继续轮询 |
| `running` | polling | 保存页数进度 |
| `converting` | polling | 继续轮询 |
| `done` | downloading | 下载结果 ZIP |
| `failed` | failed/retry | 按错误码分类 |

轮询：2s、4s、8s、15s、30s 后保持 30s，10% jitter，默认最大墙钟 30 分钟。重启后根据 `batch_id` 恢复，不重新上传。

### 7.5 Result ZIP

必须先完整保存 ZIP Blob，再解压和派生。按 [MinerU 输出文件说明](https://opendatalab.github.io/MinerU/reference/output_files/) 处理：

- `full.md` 为正式正文基线。
- `content_list.json` 用于阅读顺序、页码、图片和表格索引。
- `content_list_v2.json` 当前为开发结构，只存档，不作为唯一依赖。
- `middle.json/model.json` 用于诊断和质量检查。
- `images/` 写入 Layer 3 Derived Image Blob。

VLM 与 Pipeline 的 JSON 不完全兼容，因此 V1.1 不以 JSON 建长期 AST。

### 7.6 文件预检

校验 ResourceType、Blob 存在、SHA-256、Magic/MIME、非空、正确后缀、≤200MB、PDF 可取得时 ≤200 页，以及外部处理授权。超限不自动拆分。

## 8. Converter 设计

```python
class EvidenceConverter(Protocol):
    async def convert(self, raw_asset, blobs, context) -> ConversionResult: ...
```

- WeChat：解析 `#js_article/#js_content/cgiDataNew`，映射 Child Images，不联网补图。
- Web：`RENDERED_HTML > PRIMARY_HTML > RAW_HTTP_BODY`；成熟 Readability + Markdown；MHTML 恢复图片。
- Text：保留原文，只规范换行；`display_name` 可作为展示标题但标明来源。
- MinerU：只读取 Blob 接口；`full.md` 为基线，重写图片相对路径；JSON 只辅助索引和质量。

普通网页质量前置条件：Layer 2 应正式启用 Crawl4AI/MHTML；HTTP fallback 可处理但动态正文和图片完整性较低。

## 9. 图片、Metadata 与 Identifier

图片来源必须记录：微信 Child Image（Layer 2）、MHTML 提取图（Layer 3 派生）、MinerU `images/`（Layer 3 派生），并保留 `parent_blob_id`。

图片流程：技术过滤 → IGNORE/CONTENT/EVIDENCE → 可选 Vision → information_delta → 明确标记后写入 Markdown。

Vision Cache Key：`image_sha256 + context_sha256 + analyzer_version + prompt_version`。

时间严格区分 `published_at/updated_at/first_seen_at/fetched_at/processed_at`。未知保持 null；PDF/Word 内置时间标为 `embedded_document_metadata`，不冒充可靠发布时间。

规则提取 DOI、PMID、PMCID、NCT、ChiCTR、ORCID，并记录匹配来源和位置。

## 10. Fingerprint、幂等与缓存

Fingerprint 是规范化 JSON 的 SHA-256，至少包含 Pipeline、Converter、MinerU API/模型/OCR/表格/公式/语言、Metadata Extractor、Image Classifier、Vision Model 和 Prompt 版本。

```text
相同 asset_id + fingerprint + success/partial → 返回已有 Evidence
相同 asset_id + 新 fingerprint → 新 Run + 新 Evidence Version
新 RawAsset → 新 asset_id → 新 Evidence
```

MinerU Cache Key：`raw_blob_sha256 + model + is_ocr + formula + table + language + page_ranges`。相同文件和参数可复用 Result Archive，但分别保持 asset_id 的 Evidence 追溯。

## 11. 数据库设计

### ingestion_runs

字段：`id/asset_id/resource_type/pipeline_version/pipeline_fingerprint/converter_name/converter_version/status/attempt_count/evidence_id/warnings/error/started_at/finished_at/created_at`。Unique：`(asset_id,pipeline_fingerprint)`。

### external_parse_jobs

字段：`id/run_id/provider/api_version/batch_id/remote_task_id/data_id/trace_id/remote_state/request_options/poll_count/result_archive_blob_id/provider_error/各阶段时间`。索引：`run_id/batch_id/remote_state`。

### evidence_documents

字段：`id/evidence_id/run_id/asset_id/raw_resource_key/raw_version_no/raw_sha256/evidence_version_no/pipeline_version/pipeline_fingerprint/markdown_blob_id/yaml_blob_id/source_archive_blob_id/status/warnings/created_at`。Unique：`evidence_id`、`(asset_id,evidence_version_no)`。

### derived_artifacts

字段：`id/evidence_id/role/blob_id/parent_blob_id/sha256/mime_type/size/logical_path/producer/producer_version/created_at`。Unique：`(evidence_id,role,logical_path)`。

### vision_analysis_cache

字段：`cache_key/image_sha256/context_sha256/analyzer_version/prompt_version/result_blob_id/created_at`。

### ingestion_outbox_events

字段：`id/event_type/aggregate_id/payload_json/created_at/published_at`。EvidenceDocument 与 `evidence_document.created` 必须同事务写入。

## 12. Worker、事件与恢复

输入 `raw_asset.created`。Web/微信/Text 默认自动排队；PDF/Word 仅在允许外部处理时排队，否则 quarantined 等待确认；媒体不创建失败 Run，只显示等待转写 Provider。

输出 `evidence_document.created`：包含 `evidence_id/asset_id/raw_version_no/pipeline_version/fingerprint/status/created_at`。

故障恢复顺序：

1. 创建 Run 和 External Job。
2. 申请并流式上传。
3. 保存 `batch_id` 后轮询。
4. 完成后先保存 Result ZIP Blob。
5. 安全解包并保存派生 Blob。
6. 生成 Markdown/YAML。
7. Evidence + Outbox 同事务写入。
8. 生成只读 View，最后更新 Run。

有 `batch_id` 时恢复轮询；有 ZIP 时离线继续组装；Evidence 已入库但 View 缺失时重建。不得只依赖 FastAPI BackgroundTask。

## 13. API 与 UI

API：

```text
POST /api/v1/ingestions
GET  /api/v1/ingestions/{run_id}
GET  /api/v1/evidence
GET  /api/v1/evidence/{evidence_id}
GET  /api/v1/evidence/{evidence_id}/content
GET  /api/v1/evidence/{evidence_id}/meta
GET  /api/v1/evidence/{evidence_id}/package
POST /api/v1/evidence/{evidence_id}/reprocess
```

创建请求可包含 `asset_id/force/external_processing_allowed` 和白名单 Options；不得暴露 Token。`content` 返回 `text/markdown`，`package` 返回可读 Evidence ZIP，不默认暴露 MinerU 原始 ZIP。

UI 建议导航：`采集任务 | 原始资产 | Evidence | 系统设置`。采集状态与解析状态必须分开。显示本地阶段、MinerU 脱敏状态、页数进度、模型参数、Warnings、Evidence 版本、Markdown 预览和 RawAsset 追溯；不显示 Token 或签名 URL。

## 14. 配置与密钥

```yaml
ingestion:
  pipeline_version: "1.1.0"
  auto_process: {web_page: true, wechat_article: true, text: true, pdf: false, word: false}
  mineru:
    enabled: true
    base_url: "https://mineru.net"
    api_version: "v4"
    token_env: "MINERU_API_TOKEN"
    model_version: "vlm"
    language: "ch"
    enable_formula: true
    enable_table: true
    default_is_ocr: false
    poll_initial_seconds: 2
    poll_max_seconds: 30
    task_timeout_seconds: 1800
    max_source_bytes: 209715200
    max_result_zip_bytes: 524288000
  evidence:
    local_view_root: "./data/evidence"
    export_diagnostics: true
  images:
    max_images_per_document: 30
    max_vision_images_per_document: 10
```

Token 只来自环境变量、Keychain 或 Secret Manager；`.env` 不入 Git。日志统一脱敏 Authorization、Token、Seed 和签名 Query。

## 15. 错误码与重试

本地稳定错误码：

```text
RAW_ASSET_NOT_FOUND PRIMARY_BLOB_NOT_FOUND RAW_BLOB_HASH_MISMATCH
UNSUPPORTED_RESOURCE_TYPE EXTERNAL_PROCESSING_NOT_ALLOWED
SOURCE_FILE_EMPTY SOURCE_FILE_TOO_LARGE SOURCE_PAGE_LIMIT_EXCEEDED SOURCE_TYPE_MISMATCH
MINERU_DISABLED MINERU_AUTH_ERROR MINERU_RATE_LIMITED MINERU_REQUEST_INVALID
MINERU_UPLOAD_URL_FAILED MINERU_UPLOAD_FAILED MINERU_TASK_QUEUE_FULL
MINERU_TASK_FAILED MINERU_POLL_TIMEOUT MINERU_RESULT_DOWNLOAD_FAILED
MINERU_FILE_CONVERSION_FAILED RESULT_ARCHIVE_TOO_LARGE RESULT_ARCHIVE_INVALID
RESULT_ARCHIVE_UNSAFE MARKDOWN_NOT_FOUND MARKDOWN_EMPTY CONTENT_TOO_SHORT
IMAGE_ASSET_MISSING VISION_ANALYSIS_FAILED YAML_SERIALIZATION_FAILED
EVIDENCE_WRITE_FAILED DATABASE_ERROR UNKNOWN
```

官方错误至少映射：

| Provider | 本地 | 重试 |
|---|---|---|
| A0202/A0211 | MINERU_AUTH_ERROR | 否，告警 |
| -500/-10002 | MINERU_REQUEST_INVALID | 否 |
| -10001 | MINERU_TASK_FAILED | 是 |
| -60001 | MINERU_UPLOAD_URL_FAILED | 是 |
| -60002 | SOURCE_TYPE_MISMATCH | 否 |
| -60003/-60004 | SOURCE_FILE_EMPTY/INVALID | 否 |
| -60005 | SOURCE_FILE_TOO_LARGE | 否 |
| -60006 | SOURCE_PAGE_LIMIT_EXCEEDED | 否 |
| -60007/-60009 | MINERU_TASK_QUEUE_FULL | 长退避 |
| -60008/-60011 | MINERU_UPLOAD_FAILED | 重新申请一次 |
| -60010 | MINERU_TASK_FAILED | 最多一次 |
| -60015/-60016 | MINERU_FILE_CONVERSION_FAILED | 否，人工处理 |
| -60017/-60018/-60019 | MINERU_TASK_FAILED | 否或次日 |

HTTP timeout/429/502/503/504 最多 3 次；PUT 同 URL 最多 2 次；ZIP 下载最多 3 次。Assembler 失败优先复用已保存 ZIP，不重新调用 MinerU。

## 16. 安全与隐私

MinerU ZIP 是不可信外部输入：限制下载大小、成员数、单文件和总解压大小、压缩比；拒绝绝对路径、`..`、符号链接、设备文件和非白名单类型；隔离临时目录解压，不执行任何内容。

Result URL 仅 HTTPS 且校验配置的 CDN Host Allowlist，每次 Redirect 重新校验。预签名 URL 不持久化。

首次启用 MinerU 必须提示“文档将发送给外部解析服务”。`sensitive_source=true` 默认禁止外发。日志不输出完整 Raw 文档、Markdown 或图片。

## 17. 可观测性

日志字段：`run_id/asset_id/evidence_id/resource_type/converter/fingerprint/provider/batch_id/remote_state/trace_id/duration/error_code`。

Metrics：

```text
ingestion_runs_total{resource_type,status}
ingestion_duration_seconds{resource_type,converter}
mineru_requests_total{operation,status}
mineru_remote_state_total{state}
mineru_upload_bytes_total
mineru_poll_duration_seconds
mineru_result_download_bytes_total
evidence_markdown_bytes
quality_warning_total{warning}
identifier_found_total{type}
image_classified_total{role}
vision_analysis_total{status,role}
```

Dashboard 关注 Evidence 成功率、Partial Rate、MinerU 成功率、排队/解析时间、OCR Retry、Markdown Empty 和每日页数额度。

## 18. 代码目录

```text
src/sci_radar/ingestion/
├── api/{routes.py,schemas.py}
├── application/{ingestion_service.py,evidence_assembler.py,evidence_writer.py,pipeline_fingerprint.py}
├── domain/{enums.py,models.py,errors.py,events.py}
├── converters/{base.py,resolver.py,wechat_article.py,web_page.py,text.py,mineru_document.py}
├── providers/mineru/{client.py,schemas.py,uploader.py,poller.py,result_archive.py,error_mapper.py}
├── metadata/{title.py,source.py,time.py,language.py,identifiers.py}
├── images/{indexer.py,classifier.py,analyzer.py,cache.py,renderer.py}
├── quality/validator.py
├── repository/{ingestion_run_repository.py,external_job_repository.py,evidence_repository.py,derived_artifact_repository.py}
├── storage/{evidence_view.py,safe_zip.py}
├── queue/worker.py
├── observability/{logging.py,metrics.py}
├── config.py
└── bootstrap.py
```

测试分 unit/integration/contract/e2e/fixtures，不调用真实 MinerU 的测试使用 MockTransport 和固定 Result ZIP。

## 19. 测试策略

关键测试：Resolver、Fingerprint 稳定性、MinerU Schema/State/Error、Backoff、预检、流式上传、ZIP Slip/Bomb、`full.md` 定位、图片路径、Metadata、Quality、同 inode 只读 View、Worker 恢复、Token 脱敏。

PDF：文字型、扫描 OCR 重放、双栏、公式、表格、超限、ZIP 离线重放。Word：标题、列表、表格、图片、链接、旧 DOC、转换失败、版本重放。

边界：Web/微信不访问原站；未授权不调用 MinerU；数据库无实体 bytes；Token 不出现在任何持久化或响应；不生成 Knowledge 对象。

默认 CI 完全离线。可选 `live_mineru` 仅在显式 Token/开关下，用 1–2 页无敏感 Fixture。总覆盖率 ≥80%，幂等、密钥、ZIP、恢复关键路径 100%。

## 20. 实施阶段

1. **Phase 0 Layer 2 契约加固**：Blob 流式读取、RawAsset/Outbox 固化、外发策略；确认 Crawl4AI/MHTML。
2. **Phase 1 Ingestion 主链路**：Domain/DB/Worker，Text/微信/Web，Evidence 双视图与版本。
3. **Phase 2 MinerU**：Client/Uploader/Poller/Job 恢复、ZIP 安全、PDF/Word Converter。
4. **Phase 3 Metadata/质量**：时间、来源、Identifier、content_list、OCR Retry、Partial。
5. **Phase 4 图片**：过滤、分类、Vision、Cache、information_delta 标记。
6. **Phase 5 API/UI/Observability**：Evidence 工作台、预览、Metrics、Contract Test。
7. **Phase 6 生产化**：PostgreSQL、独立 Worker、Secret Manager、对象存储、认证审计。

每阶段独立验收，未经上一阶段通过不进入下一阶段。

## 21. Definition of Done

1. 五类 RawAsset 均有明确 Converter。
2. PDF/Word 使用 MinerU 精准 API 和预签名上传，不暴露 Blob URL。
3. 原始文件不进数据库、不修改。
4. Result ZIP 在派生前完整保存。
5. full.md、JSON、图片均形成可追溯 DerivedArtifact。
6. 每个 Evidence 有 Markdown/YAML/Assets 双视图且不复制实体。
7. Evidence 可追溯到 RawAsset、Blob、Run、Fingerprint 和 MinerU Job。
8. 相同 Asset+Fingerprint 不重复调用 MinerU；参数变化不覆盖旧版本。
9. 重启可恢复轮询或从 ZIP 继续。
10. Token/签名 URL 不进日志、DB、API、YAML、Manifest。
11. 未授权文档不外发；ZIP/URL 安全测试通过。
12. Markdown 保留标题、正文、列表、表格、公式、链接和图片。
13. 派生视觉内容与原文明示区分。
14. 未知 Metadata 保持 null，不猜测。
15. 图片失败不丢正文，正确 Partial。
16. Layer 3 不联网补原站、不生成 Knowledge 对象。
17. CI 离线稳定，覆盖率和关键路径达标。
18. API、UI、Metrics、错误码和运维说明齐全。

## 22. 已确认的实施决策

1. MinerU Token 由本地环境变量 `MINERU_API_TOKEN` 注入，不写数据库、日志、文档或 Git。
2. PDF/Word 默认不自动外发，由用户在创建 Ingestion 时显式确认。
3. 默认使用 `vlm`，开放 `pipeline` 白名单手工切换。
4. Markdown 过短且疑似扫描 PDF 时，允许 `is_ocr=true` 自动重放一次。
5. 图片 Vision 后置到 Phase 4，不阻塞本轮统一入库主链路。
6. 永久保存 MinerU Result ZIP；诊断文件默认保存在 BlobStore，Evidence 可读视图仅导出必要诊断项。
7. 本轮先补齐 Layer 2 Crawl4AI；MHTML 作为运行时能力逐步增强。
8. 本轮采用 SQLite + 单 Worker；保留未来迁移 PostgreSQL 与独立 Worker 的接口边界。

## 23. 官方资料与版本风险

- [MinerU 文档解析接口](https://mineru.net/apiManage/docs)
- [MinerU Output Files](https://opendatalab.github.io/MinerU/reference/output_files/)
- [MinerU GitHub](https://github.com/opendatalab/MinerU)

本文依据 2026-08-28 可访问的官方文档。外部限制、模型、ZIP 结构和错误码可能变化；实现必须封装 HTTP 契约、保留 Contract Fixtures、提供可选 Live Test、兼容未知字段，并把实际 Provider 参数与 Adapter 版本写入 Fingerprint。
