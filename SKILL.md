---
name: paper-to-academic-slides
description: 把科研论文/研究材料（PDF 及配套模板、起始 PPTX）转换为主线清晰、证据可追溯、可编辑、可放映的学术汇报 PPT。当用户要求“论文转 PPT/学术汇报/组会汇报/会议演示/journal club/paper to slides/research presentation”，或给出 case.json、task.md、论文 PDF 与模板要求生成带讲稿备注、图表引用、证据映射和 PDF/PNG 预览的演示文件时使用。覆盖论文主线提取、页级主张与信息分层、图表证据映射、演讲备注、模板版式适配、导出渲染与质量核查。
---

# 论文转学术 PPT（Paper to Academic Slides）

把连续叙述的论文重写为**页级离散表达**：一页一个主张，每个主张挂接论文中的图、表、章节或页码，在给定模板内排得下、讲得通、打开即可放映。这不是把段落切成行，而是一次结构重写。

## 输入与输出

- 输入目录（默认 `/input`）：`case.json`（案例标识、约束、可能的 output_contract）、`task.md`（任务说明）、论文 PDF、模板/起始 PPTX 及其他素材。
- 工作目录（默认 `/app/work`）：所有中间产物（解析结果、裁剪图、计划、渲染件）。
- 输出目录（默认 `/app/output`）：可编辑 PPTX、渲染 PDF、逐页 PNG、页级证据映射（JSON/CSV/XLSX）、`result.json`。
- 路径可用 `scripts/run.py` 的 `--input-dir/--work-dir/--output-dir` 覆盖，也可用环境变量 `SKILL_INPUT_DIR/SKILL_WORK_DIR/SKILL_OUTPUT_DIR`。

## 执行流程（7 个阶段，严格按序）

### 阶段 1-3 · 确定性准备（脚本，无需模型推理）

```bash
python3 scripts/run.py prepare
```

- `stage_intake.py`：读取 `case.json`/`task.md`，盘点全部素材并判定角色（论文/模板/起始 deck），提取冻结约束：受众、时长、**页数上限**、语言。
- `stage_extract.py`：用 PyMuPDF 解析真实 PDF——分页文本、目录、元数据、图表 caption（中英文，逐字保留）、文内引用、200 DPI 图表区域裁剪（标注示意图/数据图、本文结果/他人结果）、pdfplumber 表格行；产出 `paper.json` 与 `paper_review.md`。
- `stage_template.py`：分析真实母版/版式——幻灯片尺寸、每个版式的占位符（类型、几何）、主题色与字体、起始 deck 现有页、安全区，并渲染模板缩略图；无模板时输出中性默认规格。

### 阶段 4 · 语义规划（本技能的核心智力工作，由你完成）

1. 阅读 `paper_review.md`、`case_brief.json`、`template.json`（必要时看模板缩略图）。
2. 按 [`references/planning_prompt.md`](references/planning_prompt.md) 的要求，抽取论文主线并编写 `slide_plan.json`：
   - `spine`：问题、缺口、核心方法、关键证据、贡献、局限——按论文自身贡献组织，**不按段落顺序搬运**。
   - 每页：`role`、`title`（页级主张）、`bullets`（可讲述的短语与层级）、`figures/tables`（仅引用论文真实存在的编号）、`evidence`（图/表/章节/页码 + 逐字引文）、`notes`（不重复页面文字的讲稿）、`source_label`。
3. 结构严格遵循 [`schemas/slide_plan.schema.json`](schemas/slide_plan.schema.json)；事实与数字必须逐字来自论文，规则见 [`references/evidence_rules.md`](references/evidence_rules.md)。
4. 校验计划（阻塞性错误必须修复后再继续）：

```bash
python3 scripts/run.py validate
```

若无法完成语义规划（时间不足、解析严重缺失），运行 `python3 scripts/run.py auto` 或调用 `stage_autoplan.py` 生成确定性兜底计划，并在 `uncertainty` 中说明；不得把兜底计划冒充语义规划。

### 阶段 5 · 构建与渲染（脚本）

```bash
python3 scripts/run.py build
```

- `stage_build.py`：在真实模板/起始 deck 上构建（保留母版、版式、主题色、页脚，绝不覆盖）；优先用占位符填充，缺失时在安全区内加文本框；图片为真实图片并附逐字图注与来源标签，表格为原生 PPT 表格，讲稿写入备注；字体策略与确定性文本适配防止溢出与字体替换。
- `stage_render.py`：LibreOffice 导出 PDF，并逐页渲染 PNG；校验页数与幻灯片数一致。

### 阶段 6 · 质量核查（脚本 + 你的修复）

```bash
python3 scripts/run.py build   # 已含 qa；报告见 qa_report.json
```

`stage_qa.py` 基于**渲染后的真实 PDF** 检查：形状越界（真实溢出）、嵌入字体与替换风险、未知图表编号、证据页码越界、引文无法逐字定位、数字/单位在被引页不存在、备注缺失或与页面文字重复、页数上限。出现 error 时：先判断是计划问题还是版面问题，修复 `slide_plan.json`（或让构建脚本收缩字号），重新 `validate → build`，最多迭代 2 轮；残余问题写入 `result.json` 的 `warnings/uncertainty`。

### 阶段 7 · 封装交付

```bash
python3 scripts/run.py finalize
```

`stage_finalize.py` 汇总 `/app/output` 全部产物并写 `result.json`：`case_id`、诚实 `status`（success/partial/failed）、主线摘要、全部产物路径、逐页证据、来源清单、`uncertainty`、`warnings/errors`、`change_records`、环境与版本指纹。契约字段见 [`references/output_contract.md`](references/output_contract.md)。

## 页面组织原则（速记）

- 一页一个主张；标题即结论性陈述，不是话题名（写“X 提高了 Y 的准确率 12%”而非“结果”）。
- 单页信息密度：正文 ≤ 5 个要点、每点 ≤ 2 行；长句改写为可讲述的短语，数字与单位逐字保留。
- 页序遵循“问题 → 缺口 → 方法 → 关键证据（按论证需要而非论文顺序）→ 贡献与局限”。
- 示意图只用于解释机制，不得当实验数据；他人结果显式标注，不得与本文结果混淆。
- 备注写过渡、强调点、被问到时的补充证据（样本量、条件、局限），不复读页面文字。

## 异常处理

加密 PDF、图表无法提取、公式为图片、模板占位符不足、字体缺失、页数超限等情况的识别与处置见 [`references/exceptions.md`](references/exceptions.md)。原则：能降级则降级并记录，缺失状态如实标注，**绝不编造软件状态或科学数据**；部分结果必须明确其局限。

## 预算与稳定性

- 平台预算：64 个模型请求、900 秒、单响应 16384 tokens。语义规划目标 1 次成稿、QA 后最多 1-2 次修复，其余全部走脚本。
- 脚本均为幂等设计，可重复运行；每个变更动作写入 `change_records.json`，保证他人可按记录复跑同一版式。
- 依赖：`python-pptx`、`PyMuPDF`、`pdfplumber`、`Pillow`、`openpyxl`，以及系统 `soffice`（LibreOffice）、`pdftoppm`；详见 `requirements.txt`。
