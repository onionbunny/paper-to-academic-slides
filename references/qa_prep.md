# Q&A preparation rules

Q&A is a planned part of the deliverable, not an afterthought. The plan
contains 4–8 `qa_prep` items, each answerable with evidence already in the
materials.

## Required coverage

At minimum include one item per category; add more when the paper invites
them.

1. **mechanism** — Why does the method work? What does each component
   contribute? What is the physical/biological explanation?
2. **data** — Sample size, metrics, statistical tests, effect sizes,
   data quality, preprocessing, measurement uncertainty.
3. **controls** — Baselines, ablations, negative/positive controls,
   alternative conditions, fairness of the comparison.
4. **limitation** — Stated caveats, external validity, generalization,
   what the design cannot establish.
5. **general** — "So what?", next steps, practical/clinical implications,
   reproducibility.

## How to write each item

- `question`: a sharp, specific question a real skeptic would ask, not a
  topic heading.
- `answer_points`: 2–4 short points, each grounded in the source. Lead
  with the direct answer, then the evidence, then the scope.
- `evidence`: real figure/table/page bindings with a quote where the
  answer depends on prose.
- `backup_slide_no`: set when a backup slide supports the answer;
  otherwise null.

## Anticipation heuristics

- Every ablation in the paper maps to a controls question.
- Every non-significant or weak result maps to a data question.
- Every novel mechanism term maps to a mechanism question asking for an
  alternative explanation.
- Every cohort/sample restriction maps to a generalization question.
- Every strong headline claim maps to a "how strong is that evidence?"
  question.

## Backup slides

- Backup slides reuse real content: extra methods detail, full tables,
  supplementary figures, derivations, per-group breakdowns.
- They are built and rendered like main slides, marked `backup: true`,
  placed after the conclusion, and never counted in talk timing.
- Do not create a backup slide whose content is not in the source.

## Answer discipline

- One consistent answer per question: notes, answer points and backup
  slides must use the same numbers and hedging as the main deck.
- If the source does not answer the question, say so explicitly in the
  answer points ("not reported / not tested in this study") — never
  improvise a result.
- Keep answers within the measured scope; a question about future work is
  answered as a plan, not a claim.
