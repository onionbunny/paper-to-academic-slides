"""Deterministic fallback planner.

Runs only when no semantic slide_plan.json was produced. Unlike a naive
"copy the abstract" fallback, it builds the spine from the abstract and
limitations sections, binds every real figure/table, allocates the frozen
audience/pace budget, and writes grounded notes and Q&A. It is always
labeled ``deterministic_fallback`` so it can never masquerade as semantic
planning.
"""
from __future__ import annotations

import re
from pathlib import Path

from common import (
    ASSETS_DIR, CHANGE_RECORDS, CASE_BRIEF, PAPER_JSON, SLIDE_PLAN, TEMPLATE_JSON,
    ChangeLog, get_logger, read_json, truncate_words, write_json, has_cjk,
)

LOG = get_logger("autoplan")
SKILL_VERSION = "1.0.0"

_PROFILE_HINTS = {
    "domain_expert": ("conference", "symposium", "workshop", "session"),
    "mixed_seminar": ("seminar", "biomedical", "colloquium"),
    "committee": ("defense", "committee", "review", "panel", "evaluation"),
    "collaborator": ("group meeting", "lab meeting", "team", "progress", "update"),
    "general": ("general", "public", "interdisciplinary", "introductory"),
}

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_LABEL_PREFIX = re.compile(r"^(figure|fig\.?|table)\s*[0-9A-Za-z]+\.?\s*[:\-]?\s*", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if len(s.strip()) > 20]


def _profile(audience: str) -> str:
    low = (audience or "").lower()
    for profile, hints in _PROFILE_HINTS.items():
        if any(h in low for h in hints):
            return profile
    return "mixed_seminar"


def _strip_label(caption: str) -> str:
    return _LABEL_PREFIX.sub("", caption or "").strip().rstrip(".")


def _find_limitations(paper: dict) -> list[dict]:
    """Return limitation sentences with the real page each one lives on."""
    hits: list[dict] = []
    for lvl, title, page in paper.get("toc", []):
        if re.search(r"limitation|discussion|conclusion", title, re.I):
            text = paper["page_text"][page - 1] if 0 < page <= len(paper["page_text"]) else ""
            for s in _sentences(text):
                if re.search(r"limit|future|however|cannot|caveat|constraint", s, re.I):
                    hits.append({"text": truncate_words(s, 200), "page": page})
                if len(hits) >= 4:
                    return hits
    return hits


def _abstract_page(paper: dict) -> int:
    """Locate the page that actually contains the abstract."""
    abstract = paper.get("abstract", "")
    needle = re.sub(r"\s+", " ", abstract)[:60].lower()
    if needle:
        for i, text in enumerate(paper.get("page_text", [])):
            if needle in re.sub(r"\s+", " ", text).lower():
                return i + 1
    return 1


def _clean_context(raw: str) -> str:
    """Turn a number-index context into a clean, complete sentence."""
    ctx = re.sub(r"\s+", " ", raw or "").strip()
    tokens = ctx.split()
    # drop leading hyphenation fragments (lowercase words continued from above)
    while tokens and tokens[0][0].islower() and not tokens[0].endswith(":"):
        tokens.pop(0)
    ctx = " ".join(tokens).strip()
    # then drop leading section numbers, e.g. "3.2.1 Scaled ..."
    ctx = re.sub(r"^(\d+(?:\.\d+)*\.?\s+)+", "", ctx).strip()
    # trim an incomplete trailing clause after the last full stop
    if "." in ctx:
        head, tail = ctx.rsplit(".", 1)
        if tail.strip() and len(tail.strip()) < 45:
            ctx = head + "."
    if not ctx or not ctx[0].isupper():
        return ""
    words = re.findall(r"[A-Za-z]+", ctx)
    digits = re.findall(r"\d+", ctx)
    if len(words) < 5:
        return ""
    if digits and len(digits) > len(words) * 0.6:  # mostly number runs, not prose
        return ""
    return truncate_words(ctx, 160)


def _first_sentence(text: str, limit: int = 170) -> str:
    sents = _sentences(text)
    return truncate_words(sents[0], limit) if sents else truncate_words(text, limit)


def _numbers_for_page(paper: dict, page: int, cap: int = 2) -> list[str]:
    out = []
    seen = set()
    for item in paper.get("number_index", []):
        if item.get("page") == page and item.get("token"):
            ctx = _clean_context(item.get("context", ""))
            key = ctx[:60].lower()
            if ctx and key not in seen:
                seen.add(key)
                out.append(ctx)
        if len(out) >= cap:
            break
    return out


def _evidence_for(kind: str, fid: str, page: int, quote: str) -> dict:
    return {"kind": kind, "id": fid, "page": page, "quote": truncate_words(quote, 380)}


def _normalize_timing(slides: list[dict], budget_seconds: float) -> None:
    """Scale talk-slide timing so the total matches the real time budget."""
    talk = [s for s in slides if not s.get("backup")]
    current = sum(s["timing_seconds"] for s in talk)
    if current <= 0:
        return
    factor = budget_seconds / current
    for s in talk:
        s["timing_seconds"] = max(15, round(s["timing_seconds"] * factor))
    # absorb rounding drift on the longest slide
    drift = budget_seconds - sum(s["timing_seconds"] for s in talk)
    if talk and abs(drift) > 0:
        talk[-1]["timing_seconds"] = max(15, talk[-1]["timing_seconds"] + round(drift))


def build_plan(brief: dict, paper: dict, tpl: dict) -> dict:
    audience = brief["audience"]
    profile = _profile(audience)
    pacing = brief["pacing"]
    talk_target = min(pacing["talk_slide_target"], brief["max_slides"])
    backup_budget = max(0, brief["max_slides"] - talk_target)
    secs_per = pacing["target_seconds_per_slide"]
    lang = "zh" if paper.get("language", "").startswith("zh") or has_cjk(paper.get("title", "")) else "en"

    abstract_sents = _sentences(paper.get("abstract", ""))
    problem = truncate_words(abstract_sents[0], 220) if abstract_sents else paper.get("title", "")
    approach = next((s for s in abstract_sents
                     if re.search(r"we (propose|introduce|present|develop|describe)|here we", s, re.I)),
                    truncate_words(abstract_sents[len(abstract_sents) // 2], 220)
                    if abstract_sents else "")
    limitations = _find_limitations(paper)
    abs_page = _abstract_page(paper)

    figures = paper.get("figures", [])
    tables = paper.get("tables", [])

    # ---- main-deck slide assembly -----------------------------------------
    slides: list[dict] = []
    uncertainty: list[str] = []

    slides.append({
        "slide_no": 1, "role": "title", "backup": False,
        "title": truncate_words(paper.get("title", "Academic talk"), 100),
        "bullets": [{"text": truncate_words(paper.get("authors", ""), 200), "level": 0},
                    {"text": audience, "level": 0}],
        "figures": [], "tables": [],
        "evidence": [{"kind": "abstract", "page": abs_page, "section": "title"}],
        "evidence_strength": "background",
        "notes": (f"Open by stating the paper title and authors. Frame the talk for {audience}: "
                 "say what the question is and why it matters before any method detail."),
        "timing_seconds": max(20, int(secs_per * 0.5)),
    })

    # background / problem from abstract
    bg_bullets = [{"text": truncate_words(s, 200), "level": 0}
                  for s in abstract_sents[:2] if s]
    if bg_bullets:
        slides.append({
            "slide_no": len(slides) + 1, "role": "problem", "backup": False,
            "title": "The problem the field faces",
            "bullets": bg_bullets[:4],
            "figures": [], "tables": [],
            "evidence": [{"kind": "abstract", "page": abs_page, "quote": truncate_words(paper.get("abstract", ""), 380)}],
            "evidence_strength": "background",
            "notes": "Set up the limitation of existing approaches using the abstract's framing. "
                     "Do not introduce the new method on this slide; the gap must be felt first.",
            "timing_seconds": int(secs_per),
        })

    if approach:
        slides.append({
            "slide_no": len(slides) + 1, "role": "method", "backup": False,
            "title": "Core idea and approach",
            "bullets": [{"text": truncate_words(approach, 210), "level": 0}],
            "figures": [], "tables": [],
            "evidence": [{"kind": "abstract", "page": abs_page, "quote": truncate_words(approach, 380)}],
            "evidence_strength": "mechanistic",
            "notes": "State the central idea in one sentence before the components. Explain what "
                     "makes this approach different from the prior work just described.",
            "timing_seconds": int(secs_per),
        })

    # reserve slots: figures on main deck until budget, remainder to backup
    reserved_fixed = len(slides) + (1 if limitations else 0) + 1  # +limits +conclusion
    main_fig_cap = max(0, talk_target - reserved_fixed)
    main_figs = figures[:main_fig_cap] if main_fig_cap else figures[:max(1, talk_target - len(slides) - 2)]
    backup_figs = figures[len(main_figs):]

    for fig in main_figs:
        cap = fig.get("caption", "")
        topic = _strip_label(cap)
        # the complete caption sentence is the faithful text; the figure
        # itself carries the measured evidence, so no fragment sub-bullets
        bullets = [{"text": _first_sentence(topic), "level": 0}]
        asset_ok = fig.get("asset", {}).get("file")
        strength = "mechanistic" if fig.get("figure_type") == "schematic" else "direct"
        slides.append({
            "slide_no": len(slides) + 1,
            "role": "mechanism" if strength == "mechanistic" else "result",
            "backup": False,
            "title": truncate_words(("Architecture: " if strength == "mechanistic" else "Result: ") + topic, 70),
            "bullets": bullets[:3],
            "figures": [fig["id"]], "tables": [],
            "evidence": [_evidence_for("figure", fig["id"], fig["page"], cap)],
            "evidence_strength": strength,
            "notes": (f"Walk the audience through {fig['id']} on page {fig['page']}. Say what the "
                     "axes/components are, point to the feature that supports the claim, then restate "
                     "the takeaway. Keep the numbers on screen as they appear in the source."),
            "timing_seconds": int(secs_per * 1.2 if strength == "mechanistic" else secs_per),
            "source_label": f"{fig['id']} · p.{fig['page']}",
        })
        if not asset_ok:
            uncertainty.append(f"{fig['id']} has no usable raster asset; builder uses a placeholder.")

    # tables: first table on main deck only if budget remains
    main_tables, backup_tables = [], tables
    if len(slides) + 2 <= talk_target and tables:
        main_tables = tables[:1]
        backup_tables = tables[1:]
    for tab in main_tables:
        topic = _strip_label(tab.get("caption", ""))
        slides.append({
            "slide_no": len(slides) + 1, "role": "comparison", "backup": False,
            "title": truncate_words("Comparison: " + topic, 70),
            "bullets": [{"text": "Key rows from the source table; full table bound for review.", "level": 0}],
            "figures": [], "tables": [tab["id"]],
            "evidence": [_evidence_for("table", tab["id"], tab["page"], tab.get("caption", ""))],
            "evidence_strength": "comparative",
            "notes": (f"Use {tab['id']} on page {tab['page']} to compare conditions. Read the header "
                     "row first, then the rows that support the claim. Do not restate every cell."),
            "timing_seconds": int(secs_per),
            "source_label": f"{tab['id']} · p.{tab['page']}",
        })

    if limitations:
        slides.append({
            "slide_no": len(slides) + 1, "role": "limitation", "backup": False,
            "title": "Stated limitations and boundaries",
            "bullets": [{"text": item["text"], "level": 0} for item in limitations[:4]],
            "figures": [], "tables": [],
            "evidence": [{"kind": "section", "page": limitations[0]["page"],
                          # one contiguous source sentence, never a stitched composite
                          "quote": truncate_words(limitations[0]["text"], 380)}],
            "evidence_strength": "background",
            "notes": "Present the authors' own caveats candidly. Naming the boundary is what makes the "
                     "closing claim credible. Do not add criticisms the paper does not state.",
            "timing_seconds": int(secs_per * 0.9),
        })
    else:
        uncertainty.append("No explicit limitations section detected; limitation slide omitted.")

    # conclusion: the answer (approach) plus the strongest evidence, in that order
    conclusion_pts = []
    if approach:
        conclusion_pts.append("Answer: " + truncate_words(approach, 170))
    for fig in figures[:2]:
        conclusion_pts.append("Evidence: " + truncate_words(_strip_label(fig.get("caption", "")), 150))
    if not conclusion_pts:
        conclusion_pts = [truncate_words(problem, 180)]
    slides.append({
        "slide_no": len(slides) + 1, "role": "conclusion", "backup": False,
        "title": "Takeaway",
        "bullets": [{"text": s, "level": 0} for s in conclusion_pts[:3]],
        "figures": [], "tables": [],
        "evidence": [{"kind": "abstract", "page": abs_page, "quote": truncate_words(paper.get("abstract", ""), 380)}],
        "evidence_strength": "background",
        "notes": "Close by returning to the opening question and stating the answer the evidence "
                 "supports, keeping the same hedging as the source. Then invite questions.",
        "timing_seconds": max(25, int(secs_per * 0.7)),
    })

    # ---- backup slides -----------------------------------------------------
    backup_specs = [(f, "figure") for f in backup_figs] + [(t, "table") for t in backup_tables]
    backup_specs = backup_specs[:max(backup_budget, 0)]
    for item, kind in backup_specs:
        topic = _strip_label(item.get("caption", ""))
        slides.append({
            "slide_no": len(slides) + 1, "role": "backup", "backup": True,
            "title": truncate_words("Backup: " + topic, 70),
            "bullets": [{"text": truncate_words(topic, 200), "level": 0}],
            "figures": [item["id"]] if kind == "figure" else [],
            "tables": [item["id"]] if kind == "table" else [],
            "evidence": [_evidence_for(kind, item["id"], item["page"], item.get("caption", ""))],
            "evidence_strength": "direct" if kind == "figure" else "comparative",
            "notes": (f"Reference detail for {item['id']} (page {item['page']}), held in reserve for "
                     "questions. Only show if asked; keep the answer tied to this source."),
            "timing_seconds": int(secs_per),
            "source_label": f"{item['id']} · p.{item['page']}",
        })

    # ---- Q&A grounded in real content -------------------------------------
    qa = []
    if limitations:
        qa.append({"category": "limitation",
                   "question": "How do the stated limitations bound the strength of the conclusion?",
                   "answer_points": [i["text"] for i in limitations[:3]],
                   "evidence": [{"kind": "section", "page": limitations[0]["page"],
                                 "quote": truncate_words(limitations[0]["text"], 380)}],
                   "backup_slide_no": None})
    if figures:
        f0 = figures[0]
        qa.append({"category": "mechanism",
                   "question": f"What is the mechanism behind {f0['id']} and why does it work?",
                   "answer_points": [truncate_words(_strip_label(f0.get("caption", "")), 200)],
                   "evidence": [_evidence_for("figure", f0["id"], f0["page"], f0.get("caption", ""))],
                   "backup_slide_no": next((s["slide_no"] for s in slides if f0["id"] in s["figures"] and s["backup"]), None)})
    if tables:
        t0 = tables[0]
        qa.append({"category": "controls",
                   "question": "How fair is the comparison against the reported baselines?",
                   "answer_points": [truncate_words(_strip_label(t0.get("caption", "")), 200)],
                   "evidence": [_evidence_for("table", t0["id"], t0["page"], t0.get("caption", ""))],
                   "backup_slide_no": next((s["slide_no"] for s in slides if t0["id"] in s["tables"] and s["backup"]), None)})
    qa.append({"category": "general",
               "question": "What is the single most important takeaway?",
               "answer_points": [truncate_words(approach or problem, 200)],
               "evidence": [{"kind": "abstract", "page": abs_page}],
               "backup_slide_no": None})

    _normalize_timing(slides, brief["time_minutes"] * 60)

    plan = {
        "version": SKILL_VERSION,
        "case_id": brief["case_id"],
        "planner": "deterministic_fallback",
        "language": lang,
        "constraints": {
            "audience": audience,
            "audience_profile": profile,
            "time_minutes": brief["time_minutes"],
            "max_slides": brief["max_slides"],
            "talk_slides": len([s for s in slides if not s["backup"]]),
            "backup_slides": len([s for s in slides if s["backup"]]),
            "seconds_per_slide": secs_per,
        },
        "spine": {
            "problem": problem,
            "gap": truncate_words(abstract_sents[1], 220) if len(abstract_sents) > 1 else problem,
            "approach": truncate_words(approach, 220),
            "key_evidence": [truncate_words(_strip_label(f.get("caption", "")), 120) for f in figures[:5]],
            "contributions": [truncate_words(approach, 200)] if approach else [],
            "limitations": [i["text"] for i in limitations],
        },
        "slides": slides,
        "qa_prep": qa,
        "uncertainty": uncertainty,
        "warnings": ["Deterministic fallback planning: claim wording is caption-derived; semantic "
                     "planning gives audience-specific claim framing.",
                     "Table reconstruction is best-effort: all data cells are faithful, but a few "
                     "multi-word headers may span an adjacent column; the source page is bound for review."],
    }
    return plan


def run(work_dir: Path | None = None) -> dict:
    work_dir = Path(work_dir)
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / PAPER_JSON)
    tpl = read_json(work_dir / TEMPLATE_JSON)
    plan = build_plan(brief, paper, tpl)
    write_json(work_dir / SLIDE_PLAN, plan)
    changelog = ChangeLog(work_dir)
    changelog.add("autoplan", "deterministic plan",
                  f"{plan['constraints']['talk_slides']} talk + {plan['constraints']['backup_slides']} backup")
    LOG.info("fallback plan: %d slides (%d backup)",
             len(plan["slides"]), plan["constraints"]["backup_slides"])
    return plan


if __name__ == "__main__":
    import sys
    from common import WORK_DIR
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else WORK_DIR)
