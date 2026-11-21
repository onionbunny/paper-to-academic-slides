# Narrative patterns

A pattern is a role sequence that gives the talk a beginning, tension and
resolution. Choose one, then compress or expand it to fit the slide and
time budget. Never follow the paper's table of contents when the pattern
asks for a different order.

## P1. Gap-driven classic — default for most research talks

`title → background → problem → gap → method → result* → comparison →
limitation → conclusion`

- The gap is the hinge: everything before it motivates, everything after it
  answers.
- Order results by argumentative weight, not by figure number; the
  strongest evidence anchors the claim.
- Use for: domain_expert, committee, mixed_seminar; most CS, methods and
  empirical papers.

## P2. Hypothesis-verdict — biomedical / experimental studies

`title → clinical/scientific question → hypothesis → design & methods →
prediction → evidence* → verdict on hypothesis → limits → implication`

- State the hypothesis as a falsifiable sentence; each result slide says
  whether it supports or refutes a prediction.
- Controls and sample design appear before the first result so the evidence
  is interpretable.
- Use for: clinical, microbiome, molecular and any pre-registered /
  hypothesis-testing study; mixed_seminar and committee.

## P3. Bottleneck-build — systems / engineering / architecture

`title → task & bottleneck → core idea → component* → putting it together →
benchmark → ablation → limits → takeaway`

- Name the bottleneck concretely (cost, latency, error, scalability).
- Each component slide answers "which bottleneck does this remove"; the
  benchmark and ablation prove the claims.
- Use for: architecture, algorithms, platforms and hardware/software
  systems; domain_expert audiences.

## P4. Progress-update — collaborator / lab meetings

`title (date) → goal reminder → since last time → confirmed results → open
issues/blockers → decision needed → next steps → timeline`

- Every item is tagged confirmed / preliminary / blocked.
- The talk ends on an explicit decision or request, not a generic summary.
- Use for: collaborator profile only.

## P5. Concept-first — general / interdisciplinary

`title → why it matters (story/analogy) → the question → the idea in plain
language → signature evidence → what changes now → limits → takeaway`

- One conceptual figure carries the talk; raw tables and formulas live in
  backup.
- Use for: general profile and short keynote-style slots.

## Ordering and pacing rules

- **First 60 seconds:** the audience must know the problem and why it
  matters; no method names or acronyms before the question is set.
- **One role per slide.** A slide that is half method and half result has
  no single claim — split it.
- **Dividers** cost a slot; use them only when the talk is long enough
  (≥20 min) that the audience needs a structural reset.
- **Result chain:** each result slide's notes open with how it follows from
  the previous one, so the chain is audible, not just visible.
- **Limits before conclusion:** the limitations slide earns the right to a
  strong closing claim.
- **Backup placement:** backup slides sit after the conclusion, numbered
  consecutively, and are referenced by `qa_prep[].backup_slide_no`.
- **Time distribution (default):** title 5%, background+problem 15–20%,
  method 20–25%, results 35–45%, limits+conclusion 10–15%.
