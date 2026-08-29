# Evidence 质量审计报告（2026-08-29）

范围：`data/evidence/`（227 条已发布 + 1 条 staging），对照 `data/acquisition.db`、`data/ingestion.db`、`data/blobs/sha256/` 与 `src/sci_radar` 源码。

## 结论

**不完全符合预期。** 存储/结构层质量很好；但 8/28 中午生成的约 41% 证据在内容层面被 LLM 改写且未在 meta 中标注，违反「不做摘要、可追溯、可重放」的核心承诺；另有 4 条失败提取未告警、1 条 MinerU 证据卡在 staging。

## 符合预期的部分 ✅

| 检查项 | 结果 |
|---|---|
| 目录结构 | 227/227 均含 `content.md` + `meta.yaml`，无缺失 |
| DB↔磁盘一致 | 228 条 DB 记录（含 1 条 staging），227 条已发布，状态全部 success，`view_uri` 全部有效 |
| Blob 完整性 | 227/227 `meta.yaml.raw.sha256` 与 `blobs/sha256/` 实际文件哈希一致 |
| 本地提取忠实度（e473955e1785 / cf0e75054539 / d9f932cb3205 批次，133 条） | 与原始 HTML 的 shingle 包含率中位 0.98，0 条 <0.4 |
| 微信专项 Provider（3 篇） | 正文干净、保留 13+ 张配图硬链接 |
| MinerU PDF 解析（1 篇，Advanced Science） | 全文 + 图表 + 参考文献结构完整（但滞留 staging，见问题 3） |

## 问题 1（严重）：94 条证据内容被 LLM 改写，meta 却标记为本地转换 ❌

- 涉及批次：`pipeline_fingerprint=0c49da7d5f36`（93 条）+ `2e8eddb34a1e`（1 条），生成于 8/28 12:19–12:24。
- 证据：`ev-17a67b12`（pmc.ncbi.nlm.nih.gov/articles/PMC8723833）的 content.md 含 "One Sentence Summary"、"Key Data and Figures"、"Conclusion" 等章节，**raw HTML blob 中不存在这些文本**；正文为改写而非摘录。
- 量化：该批次与原 HTML 的包含率 p10=0.11、中位 0.61；28/93 条 <0.4（重度改写），29/93 ≥0.8（基本忠实）。`ev-f01bcf0f`（eastmoney）24 个正文句仅 8 句能在原文命中，且含「患者背景与手术过程」「结语」等自创标题。
- 合规冲突：这些 run 记录 `converter: web_page_local`、`external_processing_allowed: false`、`warnings: []` —— 外部 LLM 处理既未授权也未记录，违反 README 三条承诺（不做摘要 / 未授权用本地启发式 / 可重放可追溯）。
- 现状：当前源码已把 LLM 提取移入显式 gate（`SCI_LLM_EXTERNAL_PROCESSING_ALLOWED` + 独立 fingerprint + `_llm` converter 标注），现行代码不会再产生此类数据；但历史 94 条没有标记、没有隔离。
- 缓解因素：其中 92 个 asset 在 `e473955e1785` 批次存在并行的忠实版本，信息未丢失。

## 问题 2：失败提取未触发告警 ⚠️

- `ev-45a1794e`（guykawasaki.com）content.md 仅 67 字节页面标题，无告警。
- `ev-cabfb381`（jfdaily.com）两条版本均为 174 字节未渲染 JS 模板（`{{title}}`、`{{brTitle}}` 占位符），无告警。
- `ev-b05a41da`（neuroxess）98 字节，仅此一条有 `CONTENT_TOO_SHORT`。
- 根因：`evidence_assembler.py` 的告警阈值是 <50 字符；模板垃圾（174B）与纯标题（67B）中 67B 也应触发却未触发（该条属于 `cf0e75054539` 早期版本，告警逻辑是后来加的）。jfdaily 属 JS 渲染页面，需浏览器采集（可选依赖未安装时退化为原始 HTTP 归档）。

## 问题 3：1 条 MinerU 证据卡在 staging ⚠️

- `staging/ev-04345a3d-…-f7c7ad19392a`：run 状态 success，但从未 publish 到 `2026/08/28/`，DB 与磁盘差 1 条即源于此；API evidence 列表看不到它。
- run 记录含 `MinerUHTTPError: MinerU upload request failed` + 最终 success，疑似重试成功后 publish 步骤中断。

## 小问题

- MinerU OCR 连字丢失：fi/fl → "efect / eficient / diferentiated"；正文首行残留 "www.advancedscience.com"。
- 微信文章尾部「往期推荐」相关文章列表混入正文（e473955e1785 版本同样存在）。
- `identifiers` 全部为空（未提取 DOI/标题标识符；当前设计边界内，但影响下游层）。

## 建议

1. **隔离或重放 94 条 LLM 批次**：用现行代码以 `2e8eddb34a1e`（local）指纹重放生成忠实版本；或至少在 meta 中补注 LLM 处理事实并从默认视图排除。
2. **收紧质量告警**：阈值提高到 ~200 字符 + `{{...}}` 占位符检测；对检测到 JS 渲染页面的域名启用浏览器采集。
3. **修复 publish 中断**：重跑 `f7c7ad19392a` run 或将 staging 包移入正式目录并校验 artifact。
4. **MinerU 后处理**：恢复 fi/fl 连字、剥离页眉域名行。
