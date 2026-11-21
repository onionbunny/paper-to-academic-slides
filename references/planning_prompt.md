# 语义规划提示词（slide_plan.json 生成指南）

本文件是阶段 4 的工作提示词。目标：读取 `paper_review.md`（必要时回查 `paper.json` 与原始 PDF），产出符合 `schemas/slide_plan.json` 的计划文件。规划者是论文的“讲解架构师”，不是摘要器。

## 第一步：抽取主线（spine）

先回答六个问题，每个答案都必须能在论文中找到出处：

1. **问题（problem）**：论文针对什么现象/需求？现实或理论上的痛点是什么？
2. **缺口（gap）**：在本文之前已有方案为什么不够？这是本文存在的理由。
3. **核心方法（method）**：作者提出/采用了什么独特做法？用 1-2 句讲清机制，不抄流程细节。
4. **关键证据（key_results）**：哪些图/表/数据真正支撑主结论？按“证明力”排序，区分主结论证据与支撑性细节。
5. **贡献（contribution）**：作者自称且被证据支持的新增价值。
6. **局限（limitations）**：作者承认或证据边界显示的限制（样本量、条件、外推范围、方法假设）。

判断标准：主线必须与论文自身的贡献一致。检验方法——把 spine 讲给一个没读过论文的人听，他能否明白“为什么做、怎么做、凭什么信、有何价值”。

## 第二步：设计页序与页级主张

- 页序骨架：标题 → 背景与问题 → 缺口/动机 → 方法（可含机制示意图）→ 关键证据组（按论证逻辑排序，可重排论文图表顺序）→ 贡献与局限 → 结论。可按论文实际增减，但主线页不得缺失。
- 每页只承载**一个主张**。`title` 写结论性陈述（“方法在 N 个样本上把灵敏度提升至 92%”），不写话题名（“实验结果”）。
- 页数严格不超过冻结上限（`case_brief.constraints.max_slides`）。超限时按证明力裁剪：先合并支撑性细节，主结论证据必须保留；被删内容写入 `uncertainty`。
- 受众与时长决定深度：专业受众可保留专业术语与条件参数；跨领域受众需补一句概念解释。

## 第三步：逐页填充内容

对每页生成以下字段：

- `role`：`title | section | content | figure | table | conclusion | backup`。
- `layout_role`：期望模板版式 `title | section | content | figure | blank`，结合 `template.json` 中实际存在的版式填写。
- `bullets`：把长段落改写为可讲述的短语；每页 ≤ 5 点，每点 ≤ 2 行；用 `level`（0/1）表达层级；禁止整段照抄正文。
- `figures`：仅引用 `paper.json` 中真实存在的图编号；构建脚本会自动解析 `asset_path` 与逐字图注。示意图页要在主张中说明其“解释机制”的用途，不得把示意图当数据图。
- `tables`：引用真实表编号；小表走原生表格，大表改写为关键数值要点并在证据中保留完整定位。
- `evidence`：每页至少一条（title/section 页除外）。`kind` 为 figure/table/section/page；`page` 为论文页码；`quote` 为该页**逐字摘录**的原文片段（用于复核，不要改写）。
- `notes`：3-5 句可直接照读的讲稿，包含：与上一页的过渡、本页强调点、被问到时的补充证据（样本量、实验条件、局限）。禁止重复页面已有的文字；可用完整句、口语化。
- `source_label`：简短来源标记，如 `Fig. 3 · p.5`、`Table 1 · p.7`、`Abstract · p.1`。

## 第三步半：硬性质量门（真实论文实测总结，逐条满足）

1. **标题干净**：`title`/`claim` 不得保留 “Figure/Table/图/表 + 编号” 标签前缀，也不得以前导冒号、句号、逗号开头（如 “: The Transformer…”）。标签与编号必须连同其后的冒号/句号一起剥离。
2. **bullet 必须是完整、可讲述的短语**：禁止以悬挂的 “)”“,”或小写动词开头的残片（如 “respectively”“summarizes our results…”）；禁止把图注切碎后塞进 bullet，也禁止与 `title` 同义重复。
3. **只引用真实存在的编号**：`figures`/`tables` 的 id 必须在 `paper.json` 中存在，禁止凭空生成或为不可靠的“幽灵表/未标注块”建页。
4. **叙事而非图注罗列**：每个证据页的 `claim` 必须回答“它支撑主线的哪一点”，页与页之间靠 `notes` 过渡，不能只是把图注逐条念一遍。
5. **讲稿独立且逐页不同**：`notes` 不得复述页面文字，需包含“与上页的过渡 + 本页强调点 + 被追问时的补充（样本量/条件/局限）”；禁止跨页复用同一模板。
6. **复杂表诚实处理**：当表无框线、多行长单元格、无法可靠还原时，不要编造单元格，改为“完整表见源页 N”的指引页，并在 `uncertainty` 说明；宁可标注局限，也不输出错误数据。
7. **数字逐字**：所有数值、单位、统计量必须在引用页能逐字定位，禁止换算、四舍五入或单位替换。
8. **标题页信息完整**：标题与作者取 `paper.json`；若作者无法可靠识别，在 `uncertainty` 记录，不得留空白标题页。

## 第四步：语言与事实规则

- 计划语言与论文一致（中文论文用中文，英文论文用英文）；双语要求以 `task.md` 为准。
- 所有数字、单位、方法名、材料名、统计量必须逐字来自论文，禁止换算、四舍五入或同义替换；详见 `evidence_rules.md`。
- 区分本文结果与他人结果：caption 含 et al./reproduced/adapted/来源/引用 等标记时，`notes` 中必须说明这是引用证据。
- 无法从材料确认的内容，写入 `uncertainty`，禁止用先验知识补造。

## 输出骨架

```json
{
  "version": "1.0",
  "case_id": "从 case_brief 复制",
  "planner": "semantic_agent",
  "language": "zh|en",
  "constraints": { "max_slides": 12, "minutes": 10, "audience": "..." },
  "spine": {
    "problem": "...", "gap": "...", "method": "...",
    "key_results": ["..."], "contribution": "...", "limitations": ["..."]
  },
  "slides": [
    {
      "slide_no": 1,
      "role": "figure",
      "layout_role": "figure",
      "title": "结论性主张（一句话）",
      "claim": "与 title 同义的完整陈述",
      "bullets": [{"text": "可讲述的短语", "level": 0}],
      "figures": [{"id": "3"}],
      "tables": [],
      "evidence": [{"kind": "figure", "id": "3", "page": 5, "section": "Results",
                    "quote": "论文该页逐字原文片段"}],
      "notes": "不重复页面文字的讲稿……",
      "source_label": "Fig. 3 · p.5"
    }
  ],
  "uncertainty": []
}
```

写完后运行 `python3 scripts/run.py validate`；任何 error（未知编号、页码越界、超页数）必须修复后才能进入构建阶段。
