# 输出契约（result.json 与 /app/output 结构）

## 目录结构

```text
/app/output/
├── result.json                     # 主结果契约（必含 case_id）
├── <case_id>_academic_deck.pptx    # 可编辑演示文件（真实母版/版式）
├── <case_id>_academic_deck.pdf     # 渲染 PDF（复核用）
├── previews/
│   └── slide_01.png ...            # 逐页 PNG 预览
├── evidence_map.json               # 页级证据映射（机器可读）
├── evidence_map.csv                # 同内容表格（带 BOM，Excel 可直接打开）
├── evidence_map.xlsx               # 彩色表头的人工复核表
├── slide_plan.json                 # 页级计划（主张/要点/证据/备注）
├── qa_report.json                  # 质量核查报告
└── paper.json                      # 论文结构化解析结果
```

## result.json 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `case_id` | string | 从 case.json 原样复制；缺失时使用输入目录名 |
| `status` | string | `success`（QA 无 error）/ `partial`（存在告警或降级）/ `failed`（主流程无法完成） |
| `title` | string | 论文标题 |
| `summary` | object | `spine`（六要素）、`slide_count`、`cap`、`language`、`audience`、`minutes` |
| `artifacts` | object | 全部产物路径：`editable_presentation`、`pdf_preview`、`png_previews[]`、`evidence_map{json,csv,xlsx}`、`slide_plan`、`qa_report`、`paper_extraction` |
| `evidence` | array | 逐页证据行：`slide_no/role/claim/source_label/sources[]`，每个 source 含 `kind/source_id/page/section/verbatim_quote` 及示意图/引用标记 |
| `sources` | array | 输入素材清单（路径、类型、角色、大小） |
| `uncertainty` | array | 无法确认或被裁剪的内容，逐条说明 |
| `warnings` | array | QA 与摄入阶段的全部告警（自然语言） |
| `errors` | array | 阻塞性问题；success 时为空 |
| `change_records` | array | 每个变更动作的时间戳、阶段、动作、细节，支持复跑同一版式 |
| `environment` | object | Python 版本、平台、python-pptx 版本、soffice 版本、使用的模板、幻灯片尺寸、嵌入字体 |
| `output_contract_version` | string | 当前契约版本 `1.0` |

若 case.json 自带 `output_contract` 键，其字段会以 `setdefault` 方式合并进 result.json，不覆盖本契约字段。

## 状态判定规则

- `success`：deck 成功保存、PDF 页数与幻灯片数一致、QA 无 error（warnings 允许存在但需可见）。
- `partial`：渲染失败、字体替换风险、证据降级（区域裁剪/整页图/OCR）、页数裁剪、数字/引文匹配告警等任一情况；必须在 `uncertainty/warnings` 写清局限。
- `failed`：论文无法读取且无任何可交付内容；仍输出 result.json 说明原因，不生成空白 PPT 冒充成果。

## 诚实性要求

- 不虚构 benchmark 分数、模型版本、运行时间或工具可用性；版本信息由脚本实时探测。
- 不提交预制答案或正式测试输出；`examples/` 中的文件明确标注为示例。
- 运行时二进制产物（PPTX/PDF/PNG）独立保留，与 result.json 的有效性互不影响。
