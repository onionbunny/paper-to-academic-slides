# Exception and failure handling

The skill never fabricates its way out of a problem. It detects the
condition, records it in `uncertainty` / change records, degrades to a
real artifact, and sets the honest run status.

## Status semantics

- **complete** — deck, notes, evidence map and rendered previews all
  produced; every claim bound; no unresolved blocker.
- **partial** — usable deck produced, but one or more elements are
  degraded (missing asset, fragmented table, OCR-only pages, preview
  failures). The limits are listed explicitly.
- **blocked** — the core task cannot be done (e.g., no readable paper).
  Outputs still include `result.json` explaining the blocker.

## Material-level conditions

| Condition | Detection | Behavior |
|---|---|---|
| Encrypted / password PDF | PyMuPDF authentication fails | Try empty password; on failure mark the material `blocked`, do not invent text, run ends `blocked` or `partial` if other materials suffice |
| Scanned / image-only page | extracted text < 120 chars on a page | Run real OCR (tesseract, chi_sim+eng); tag section `ocr:true`, lower confidence, disclose in uncertainty |
| Missing figure asset | no graphic region or raster extractable | Keep figure binding + caption; builder inserts explicit placeholder slide element; never substitute a generated image |
| Low-resolution image | extracted pixel width below threshold | Use it anyway, tag `low_resolution`, warn in uncertainty and QA |
| Fragmented/misaligned table | table reconstruction yields sparse columns | Keep all words and source page; builder renders best-effort grid; QA flags it; no manual number "repair" |
| Caption/figure pairing ambiguous | multiple captions compete for one region | Deterministic pairing rules decide; record the alternative in uncertainty |
| Missing abstract/title/authors | no labeled block | Use documented fallback heuristics; if still absent, leave field empty and note it |
| Supplementary numbering | S1, S2 style labels | Treated as real figures/tables; source label keeps the "S" prefix |

## Template and font conditions

- **No template provided** — use the deterministic `scholar-default`
  theme documented in `default_theme.md`; only installed fonts are used.
- **Template/content conflict** (content overflows a layout, missing
  placeholder, conflicting color) — builder falls back to safe-area
  geometry on the same theme rather than dropping content; the conflict is
  logged.
- **Missing font** — `resolve_font` maps to a real installed family with
  CJK fallback; the substitution is recorded. Never embed a font the
  environment does not have.
- **Starting deck provided** — edit the existing deck in place: preserve
  its size, master and theme; add/adapt slides using its layouts instead
  of replacing the theme.

## Rendering conditions

- **LibreOffice export fails** — retry once headless with a fresh profile;
  if it still fails, keep the editable PPTX, mark preview fields null,
  status `partial`.
- **PDF rendered but PNG step fails** — keep the PDF and any PNGs already
  produced; list the failed page numbers.
- **Overflow detected in QA** — shrink content, split the slide or move
  detail to backup, then re-render; each repair is a change record.

## Budget and time conditions

- Approaching the 64-request / 900-second budget: stop improving,
  finalize the current plan and artifacts, and mark degraded elements.
- A single stage errors: record the error, run the documented fallback
  (e.g., deterministic planner), set `planner` truthfully, and continue.
- Deterministic fallback plans are labeled `deterministic_fallback`; they
  must never be presented as semantic plans.

## Universal rule

When in doubt, preserve the editable artifact and the source pointer,
describe the limit in plain language, and choose the lower status. A
reviewer should always be able to tell what is evidence, what is missing,
and why.
