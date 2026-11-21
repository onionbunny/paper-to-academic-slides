"""Validation / normalization for the agent-authored slide plan.

The agent writes slide_plan.json following schemas/slide_plan.schema.json.
This tool verifies structure, resolves figure assets from paper.json when only
an id is given, enforces the organizer-frozen slide cap and returns a precise
issue list. Exit code is non-zero on blocking errors so the agent must repair.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    CASE_BRIEF,
    PAPER_JSON,
    SLIDE_PLAN,
    WORK_DIR,
    get_logger,
    read_json,
    write_json,
)

log = get_logger("plan_tools")
REQUIRED_SLIDE_FIELDS = ("role", "title")
ROLES = {"title", "section", "content", "figure", "table", "conclusion", "backup"}


def validate(work_dir: Path, repair: bool = True) -> dict:
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / PAPER_JSON)
    plan_path = work_dir / SLIDE_PLAN
    plan = read_json(plan_path)
    errors, warnings = [], []

    if not isinstance(plan, dict) or "slides" not in plan:
        raise ValueError("slide_plan.json must be an object containing a 'slides' array")
    slides = plan["slides"]
    if not isinstance(slides, list) or not slides:
        errors.append("slides must be a non-empty array")

    fig_index = {f["id"]: f for f in paper.get("figures", [])}
    tbl_index = {t["id"]: t for t in paper.get("tables", [])}
    cap = (brief.get("constraints") or {}).get("max_slides")

    if cap and len(slides) > cap:
        errors.append(f"slide count {len(slides)} exceeds frozen cap {cap}; merge or drop slides")

    normalized = []
    for i, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            errors.append(f"slide #{i} is not an object")
            continue
        for field in REQUIRED_SLIDE_FIELDS:
            if not slide.get(field):
                errors.append(f"slide #{i} missing required field '{field}'")
        if slide.get("role") not in ROLES:
            warnings.append(f"slide #{i}: unknown role '{slide.get('role')}', defaulting to 'content'")
            slide["role"] = "content"
        slide["slide_no"] = i
        slide.setdefault("layout_role", "figure" if slide["role"] == "figure" else
                         "title" if slide["role"] == "title" else "content")
        slide.setdefault("claim", slide.get("title", ""))
        slide.setdefault("bullets", [])
        slide.setdefault("figures", [])
        slide.setdefault("tables", [])
        slide.setdefault("evidence", [])
        slide.setdefault("notes", "")
        slide.setdefault("source_label", "")

        if not slide["notes"].strip():
            warnings.append(f"slide #{i} has no speaker notes")
        if slide["role"] not in ("title", "section") and not slide["evidence"]:
            warnings.append(f"slide #{i} has no evidence locator")

        for ref in slide["figures"]:
            fid = str(ref.get("id", ""))
            if fid not in fig_index:
                errors.append(f"slide #{i} references unknown figure id '{fid}'")
                continue
            src = fig_index[fid]
            if not ref.get("asset_path"):
                ref["asset_path"] = src["asset_path"]
            ref.setdefault("caption_verbatim", src.get("caption", ""))
            ref.setdefault("is_schematic", src.get("is_schematic", False))
            ref.setdefault("is_cited_work", src.get("is_cited_work", False))
        for ref in slide["tables"]:
            tid = str(ref.get("id", ""))
            if tid not in tbl_index:
                errors.append(f"slide #{i} references unknown table id '{tid}'")
        for ev in slide["evidence"]:
            page = ev.get("page")
            if page is not None and not (1 <= int(page) <= paper["page_count"]):
                errors.append(f"slide #{i} evidence page {page} outside paper (1..{paper['page_count']})")
            ev.setdefault("kind", "page")
        normalized.append(slide)

    plan["slides"] = normalized
    plan["case_id"] = brief.get("case_id")
    plan.setdefault("language", paper.get("language"))
    plan.setdefault("spine", {})
    plan.setdefault("uncertainty", [])
    plan.setdefault("constraints", brief.get("constraints"))

    report = {"errors": errors, "warnings": warnings,
              "slide_count": len(normalized), "cap": cap}
    write_json(work_dir / "plan_validation.json", report)
    if repair and not errors:
        write_json(plan_path, plan)
    log.info("validation: %d errors, %d warnings, %d slides (cap %s)",
             len(errors), len(warnings), len(normalized), cap)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    report = validate(Path(args.work_dir), repair=not args.no_write)
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
