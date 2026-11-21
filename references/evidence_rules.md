# Evidence rules

The skill is judged on whether every on-slide claim can be located in the
source. These rules define what counts as evidence, how strong it is, and
how claims may be worded.

## Evidence strength (the `evidence_strength` field)

- **direct** — the claim is a measured result shown in a figure or table on
  the bound page (e.g., a reported BLEU score, an observed regression
  coefficient, a measured yield). Use for result slides.
- **comparative** — the claim compares two or more real conditions,
  baselines or groups, and both sides are in the source (ablation,
  baseline table, treatment vs control).
- **mechanistic** — the claim explains why, using the paper's proposed
  mechanism or causal pathway. The paper may propose rather than prove;
  keep the wording as the authors do ("suggests", "is consistent with").
- **background** — established context, definitions, or the problem
  framing. Background claims still need a page, but never carry a headline
  result.

A slide may mix strengths; assign the strength of its headline claim.
Never label a mechanistic interpretation as a direct result.

## What is valid evidence

- A figure id with a real `page` and a caption that supports the claim.
- A table id with rows containing the exact value being quoted.
- A section/page with a short verbatim `quote` (≤400 chars) for prose
  claims, contributions or limitations.
- The abstract, bound as kind `abstract`, for framing and headline claims
  only — not for detailed numbers that the results section states
  differently.

Invalid evidence: reference-list entries, author affiliations, journal
stamps, cover pages, web links, and anything outside the provided
materials.

## Wording rules

- **Numbers are copied, never rounded or recomputed.** If the slide needs
  a comparison, state the two source values; do not compute a new
  percentage or delta unless the paper states it.
- **Names, model labels, gene/enzyme names, units and formulas are
  copied verbatim**, including superscripts/subscripts where the
  extraction preserves them.
- Prose may be paraphrased for brevity, but direction and scope cannot
  change: a correlation must not become a cause; "associated with" must
  not become "causes"; a sample must not become a population.
- Hedging words are evidence too. If the source says "may", "suggests" or
  "in this cohort", the slide keeps the hedge.
- A claim that combines two sources lists both evidence entries.

## Figure and table binding

- Bind the exact id; the builder places the real cropped asset and prints
  the source label (e.g., "Fig. 3 · p. 6").
- The claim must match the visual: do not bind a figure to a conclusion it
  does not show, even if the surrounding prose makes the point.
- Tables are best-effort extractions. Quote the rows that support the
  claim; if a cell is garbled or columns are misaligned, disclose it in
  `uncertainty` and keep the source page so a reviewer can check the
  original.
- When an asset is missing or low-resolution (`asset_ok: false`), the
  slide still binds the evidence; the builder renders the caption and an
  explicit placeholder — never a generated or substituted image.

## Verdict recorded in the final evidence map

Each slide's `verbatim_check` is set during QA:

- **verified** — every claim and number matches the bound source.
- **paraphrased** — faithful rewording; direction and scope unchanged.
- **flagged** — unsupported or inconsistent; the QA report names the
  problem and the run status is downgraded.
- **no_claim** — structural slides (title, divider) with no scientific
  assertion.
