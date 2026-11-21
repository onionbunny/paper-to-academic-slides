# Output contract

Runtime paths (overridable by `SKILL_INPUT_DIR`, `SKILL_WORK_DIR`,
`SKILL_OUTPUT_DIR`):

- Inputs: `/input/case.json`, `/input/task.md`, and materials referenced by
  `case.json`.
- Working files: `/app/work/` — deterministic intermediates, kept for
  review.
- Final deliverables: `/app/output/` — everything the evaluator inspects.

## `/app/work` layout

```
/app/work/
  case_brief.json        # stage_intake: frozen constraints & inventory
  paper.json             # stage_extract: full structured extraction
  paper_review.md        # human-readable extraction review
  template.json          # stage_template: theme/layout geometry
  assets/                # real figure crops (fig1_p3.png, ...)
  slide_plan.json        # semantic plan (or deterministic fallback)
  change_records.json    # append-only audit trail
```

## `/app/output` layout

```
/app/output/
  result.json            # required, schema: schemas/result.schema.json
  deck.pptx              # editable, master/layout-based presentation
  deck.pdf               # real rendered export (null if render failed)
  speaker_notes.md       # notes also embedded in pptx notes slides
  evidence_map.md        # per-slide source ledger
  previews/
    slide_01.png ...     # one real rendered PNG per slide
```

## `result.json` requirements

- Includes the original `case_id` and every field in
  `schemas/result.schema.json`.
- `status` is one of `complete | partial | blocked` with `status_detail`
  explaining anything below complete.
- `artifacts` lists paths relative to the output directory; a render
  failure is reported as `null`, never as a fake path.
- `evidence_map` mirrors the deck: one entry per slide with role, title,
  bound evidence and `verbatim_check`.
- `sources` lists every material actually used with role, kind, real
  `sha256` and page count.
- `rendering` names the real engine, fonts and theme and carries QA
  findings.
- `uncertainty`, `limitations` and `change_records` are honest arrays;
  empty arrays are allowed only when genuinely nothing applies.
- `skill` records planner, timestamps and duration; benchmark scores are
  never invented.

## Completeness checklist

1. `deck.pptx` opens, is editable, and uses the provided template/master
   when one exists.
2. `deck.pdf` and every `previews/slide_*.png` are real renders of the
   same deck, not mock images.
3. Every slide's claims trace through `evidence_map` to a page/figure/
   table in the source.
4. Speaker notes exist for every talk slide, both embedded and in the
   notes file.
5. Any degraded element is named in `result.json` and the status reflects
   it.
