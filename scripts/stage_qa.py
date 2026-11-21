"""Post-render quality assurance.

Checks are deterministic and auditable:
  * evidence completeness and binding against the real extraction;
  * numeric tokens on slides must appear in the source;
  * shape geometry stays inside the slide (overflow detection);
  * notes exist and are not mere copies of the bullets;
  * rendered preview count matches the deck; timing fits the budget.
Outputs qa_report.json and the per-slide verbatim verdicts reused by the
final evidence map.
"""
from __future__ import annotations

import re
from pathlib import Path

from common import (
    QA_REPORT, CASE_BRIEF, PAPER_JSON, SLIDE_PLAN, TEMPLATE_JSON,
    ChangeLog, get_logger, read_json, normalize_text, write_json,
)

LOG = get_logger("qa")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_NON_CLAIM_ROLES = {"title", "divider"}


def _source_blob(paper: dict) -> str:
    parts = [paper.get("abstract", ""), " ".join(paper.get("page_text", []))]
    parts += [c.get("caption", "") for c in paper.get("captions", [])]
    return normalize_text(" ".join(parts))


def _check_overflow(deck_path: Path, tpl: dict) -> list[str]:
    from pptx import Presentation
    prs = Presentation(str(deck_path))
    w, h = prs.slide_width, prs.slide_height
    findings = []
    for i, slide in enumerate(prs.slides):
        for shape in slide.shapes:
            if shape.left is None or shape.top is None:
                continue
            try:
                if shape.left < -5000 or shape.top < -5000 or \
                        shape.left + shape.width > w + 5000 or \
                        shape.top + shape.height > h + 5000:
                    findings.append(f"slide {i + 1}: shape '{shape.shape_type}' overflows slide bounds")
            except Exception:
                continue
    return findings


def _check_slide(spec: dict, paper: dict, source_blob: str, planner: str) -> tuple[list[str], str]:
    issues = []
    role = spec["role"]
    if role in _NON_CLAIM_ROLES:
        return issues, "no_claim"

    if not spec.get("evidence"):
        issues.append("missing evidence binding")
    fig_ids = {f["id"] for f in paper.get("figures", [])}
    tab_ids = {t["id"] for t in paper.get("tables", [])}
    for fid in spec.get("figures", []):
        if fid not in fig_ids:
            issues.append(f"bound figure {fid} absent from extraction")
    for tid in spec.get("tables", []):
        if tid not in tab_ids:
            issues.append(f"bound table {tid} absent from extraction")
    for ev in spec.get("evidence", []):
        if not (1 <= ev.get("page", 0) <= paper.get("page_count", 0)):
            issues.append("evidence page outside paper range")

    # every number spoken on the slide must be present in the source
    bullet_text = " ".join(b["text"] for b in spec.get("bullets", []))
    unknown = [n for n in _NUM_RE.findall(bullet_text) if n not in source_blob]
    if unknown:
        issues.append(f"numbers not located in source: {sorted(set(unknown))[:6]}")

    notes = spec.get("notes", "")
    if len(notes) < 20:
        issues.append("speaker notes missing or too short")
    elif normalize_text(notes) == normalize_text(bullet_text):
        issues.append("notes duplicate the bullets")

    if issues:
        return issues, "flagged"
    return issues, ("paraphrased" if planner == "deterministic_fallback" else "verified")


def run(work_dir=None, output_dir=None):
    from common import OUTPUT_DIR, WORK_DIR
    work_dir = Path(work_dir or WORK_DIR)
    output_dir = Path(output_dir or OUTPUT_DIR)
    plan = read_json(work_dir / SLIDE_PLAN)
    paper = read_json(work_dir / PAPER_JSON)
    tpl = read_json(work_dir / TEMPLATE_JSON)
    render = read_json(output_dir / "render.json", default={})
    source_blob = _source_blob(paper)

    findings = []
    verdicts = []
    for spec in plan["slides"]:
        issues, verdict = _check_slide(spec, paper, source_blob, plan.get("planner"))
        for issue in issues:
            findings.append(f"slide {spec['slide_no']} ({spec['role']}): {issue}")
        verdicts.append({"slide_no": spec["slide_no"], "verbatim_check": verdict})

    deck_path = output_dir / "deck.pptx"
    if deck_path.exists():
        findings += _check_overflow(deck_path, tpl)

    if render.get("pages") and render["pages"] != len(plan["slides"]):
        findings.append(f"preview count {render['pages']} != planned slides {len(plan['slides'])}")
    if not render.get("ok"):
        findings.append(f"rendering not fully successful: {render.get('error', 'unknown')}")

    talk = [s for s in plan["slides"] if not s.get("backup")]
    total = sum(s.get("timing_seconds", 0) for s in talk)
    budget = plan["constraints"]["time_minutes"] * 60
    if abs(total - budget) > budget * 0.1:
        findings.append(f"talk timing {total}s outside ±10% of {budget}s budget")

    if len(plan["slides"]) > plan["constraints"]["max_slides"]:
        findings.append("slide count exceeds max_slides")

    report = {
        "findings": findings,
        "verdicts": verdicts,
        "slides_checked": len(plan["slides"]),
        "render_ok": bool(render.get("ok")),
        "timing_total_seconds": total,
        "passed": not findings,
    }
    write_json(output_dir / QA_REPORT, report)
    ChangeLog(work_dir).add("qa", "checks", f"{len(findings)} findings")
    LOG.info("QA: %d findings", len(findings))
    return report


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    run(args[0] if args else None, args[1] if len(args) > 1 else None)
