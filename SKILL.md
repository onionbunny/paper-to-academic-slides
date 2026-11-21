---
name: academic-slide-organizer
description: Organize a fixed research paper or research materials into an audience-aware, evidence-traceable, editable academic talk deck (PPTX with embedded speaker notes, per-slide evidence map, PDF/PNG rendered previews and result.json). Use when converting source materials into a presentation for a specified audience, time limit and slide budget, including template or starting-deck adaptation. Do not use for paper deep-reading, manuscript writing or producing new figures.
version: 1.0.0
---

# Academic slide organizer

## What this skill does

Turns the source materials listed in `/input/case.json` into a talk that is
ready to present:

- an **editable** `deck.pptx` built on the real master/layouts when a
  template or starting deck is supplied, otherwise on the deterministic
  `scholar-default` theme;
- **speaker notes** embedded on every slide plus a `speaker_notes.md`;
- a per-slide **evidence map** binding every claim to a real figure, table,
  section or page, exported as `evidence_map.md`;
- a real rendered `deck.pdf` and one PNG per slide under `previews/`;
- `result.json` with status, sources (with hashes), uncertainty, change
  records and limitations.

The five content directions are all covered: report goal, audience
adaptation, narrative order, per-slide key information, and Q&A preparation.

## Inputs

- `/input/case.json` — `case_id`, `audience`, `time_minutes`, `max_slides`,
  and `materials` (paper, and optionally a template/starting deck, docs,
  images or data files).
- `/input/task.md` — the task text.
- All paths in the skill are overridable for local testing via
  `SKILL_INPUT_DIR`, `SKILL_WORK_DIR`, `SKILL_OUTPUT_DIR`.

## Workflow

### 1. Deterministic preparation

Run:

```
python3 scripts/run.py prepare
```

This produces, in the work directory (`/app/work`): `case_brief.json`
(frozen constraints, material inventory, pacing), `paper.json` and
`paper_review.md` (real extraction: title, authors, abstract, sections,
figures with cropped assets, tables with rows, mentions, number index), and
`template.json` (slide size, safe area, theme, fonts, layouts).

### 2. Semantic planning (the agent step)

Read `references/planning_prompt.md` in full and follow it, together with:

- `references/audience_profiles.md` — map the audience to one of five
  profiles and fix the decision goal, background depth and claim strength;
- `references/narrative_patterns.md` — choose a story arc and allocate
  roles/timing;
- `references/evidence_rules.md` — evidence strength and verbatim rules;
- `references/qa_prep.md` — anticipated questions and backup slides.

Write `/app/work/slide_plan.json` conforming to
`schemas/slide_plan.schema.json`. Hard rules: no invented facts or numbers;
every non-title slide binds real evidence with a real page; one claim per
slide; notes are spoken prose, not bullet duplicates; slide count and total
timing fit the frozen budget. Validate with:

```
python3 scripts/run.py validate
```

### 3. Build, render, QA and finalize

Run:

```
python3 scripts/run.py build
```

The builder opens the provided template/starting deck preserving its
master, theme and size, or uses `scholar-default`; places real figure
images and native editable tables; embeds notes; and marks backup slides.
LibreOffice then renders the PDF and PyMuPDF the per-slide PNGs. The QA
stage checks evidence binding, numeric tokens against the source, shape
overflow, notes, preview count and timing. Finalize writes
`/app/output/result.json` per `schemas/result.schema.json`.

### Fully deterministic mode

If no semantic plan is written, the following still produces a usable,
honestly labeled deck:

```
python3 scripts/run.py all
```

This uses `stage_autoplan.py` (planner field `deterministic_fallback`),
which builds the spine from abstract/introduction/discussion, binds every
real figure and table, normalizes talk timing to the real budget, and
generates grounded Q&A. It must never be presented as semantic planning.

## Outputs (`/app/output`)

`result.json` (with the original `case_id`), `deck.pptx`, `deck.pdf`,
`speaker_notes.md`, `evidence_map.md`, and `previews/slide_*.png`. See
`references/output_contract.md` for the full contract and completeness
checklist.

## Status and exception handling

Status is one of `complete`, `partial`, `blocked`. Encrypted PDFs, scanned
pages (real OCR), missing or low-resolution assets, fragmented tables,
missing fonts, template conflicts, render failures and budget/time pressure
are detected and handled per `references/exceptions.md`: the skill keeps
the editable artifact and source pointer, degrades to a real placeholder,
and discloses the limit rather than fabricating data or software state.

## Boundaries

In scope: organizing already-available materials into a speakable,
evidence-bound, template-compliant deck. Out of scope: paper deep-reading,
manuscript writing, and generating new figure material. Benchmark scores
are never invented; only real artifacts and honest records are reported.
