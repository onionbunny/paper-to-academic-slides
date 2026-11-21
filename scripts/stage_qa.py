"""Stage 6 — quality assurance against the real rendered PDF.

Combines render-level verification with source-level fidelity checks:
  * text/image shapes crossing the slide bounds (true overflow detection)
  * embedded fonts vs expected policy, CJK coverage and substitution risk
  * unknown figure/table references and evidence pages outside the paper
  * evidence quotes that cannot be located on the cited page
  * numbers/units on slides that do not appear verbatim on the cited page
  * missing or text-duplicating speaker notes
  * frozen slide cap compliance
"""
from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

import fitz

from common import (
    CASE_BRIEF,
    QA_REPORT,
    RENDER_JSON,
    SLIDE_PLAN,
    WORK_DIR,
    ChangeLog,
    get_logger,
    has_cjk,
    normalize_text,
    read_json,
    write_json,
)
from stage_autoplan import page_texts

log = get_logger("qa")
TOL_PT = 3.0
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|kg|mg|µg|ug|ng|mL|ml|L|mm|cm|nm|eV|Da|kDa|mol|s|ms|min|h|p|n|°C|倍|个|例|岁|天|年)?(?![A-Za-z])")
FONT_BASE = {
    "Noto Sans CJK SC": "notosanscjk",
    "Noto Serif CJK SC": "notoserifcjk",
    "Carlito": "carlito",
    "Liberation Sans": "liberationsans",
    "Liberation Serif": "liberationserif",
    "Caladea": "caladea",
}


def _render_overflow(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    issues = {}
    for i, page in enumerate(doc, start=1):
        page_issues = []
        w, h = page.rect.width, page.rect.height
        for block in page.get_text("blocks"):
            x0, y0, x1, y1 = block[:4]
            if x0 < -TOL_PT or y0 < -TOL_PT or x1 > w + TOL_PT or y1 > h + TOL_PT:
                page_issues.append({"type": "text_overflow", "bbox": [round(v, 1) for v in (x0, y0, x1, y1)],
                                    "text": block[4][:80]})
        # use actual displayed rectangles (get_image_info reports pre-crop blip rects)
        for img in page.get_images(full=True):
            xref = img[0]
            for r in page.get_image_rects(xref) or []:
                b = (r.x0, r.y0, r.x1, r.y1)
                if b[0] < -TOL_PT or b[1] < -TOL_PT or b[2] > w + TOL_PT or b[3] > h + TOL_PT:
                    page_issues.append({"type": "image_overflow",
                                        "bbox": [round(v, 1) for v in b]})
        if page_issues:
            issues[str(i)] = page_issues
    doc.close()
    return issues


def _font_check(render: dict, build_report: dict, plan: dict) -> list[str]:
    warnings = []
    embedded = " ".join(render["embedded_fonts"]).lower()
    policy = build_report.get("font_policy", {})
    for key in ("latin", "ea"):
        family = policy.get(key)
        if not family:
            continue
        base = FONT_BASE.get(family, family.lower().replace(" ", ""))
        if base not in embedded:
            warnings.append(f"expected font '{family}' not found among embedded subsets; substitution risk")
    cjk_content = plan.get("language") == "zh"
    if cjk_content and "cjk" not in embedded and "noto" not in embedded:
        warnings.append("CJK content but no CJK font embedded; glyphs may render as tofu")
    return warnings


def _slide_text(plan_slide: dict) -> str:
    parts = [plan_slide.get("title", ""), plan_slide.get("claim", "")]
    parts += [b.get("text", "") for b in plan_slide.get("bullets", [])]
    parts += [f.get("caption_verbatim", "") for f in plan_slide.get("figures", [])]
    return " ".join(p for p in parts if p)


def _evidence_checks(plan: dict, paper: dict, page_text: list[str]) -> list[dict]:
    findings = []
    fig_ids = {f["id"] for f in paper.get("figures", [])}
    tbl_ids = {t["id"] for t in paper.get("tables", [])}
    norm_pages = [normalize_text(t) for t in page_text]
    for slide in plan["slides"]:
        no = slide["slide_no"]
        for ref in slide.get("figures", []):
            if str(ref.get("id")) not in fig_ids:
                findings.append({"slide": no, "level": "error", "issue": f"unknown figure id {ref.get('id')}"})
        for ref in slide.get("tables", []):
            if str(ref.get("id")) not in tbl_ids:
                findings.append({"slide": no, "level": "error", "issue": f"unknown table id {ref.get('id')}"})
        for ev in slide.get("evidence", []):
            page = ev.get("page")
            if page is None:
                continue
            if not (1 <= int(page) <= paper["page_count"]):
                findings.append({"slide": no, "level": "error", "issue": f"evidence page {page} out of range"})
                continue
            quote = ev.get("quote", "")
            if quote and normalize_text(quote)[:40] not in norm_pages[int(page) - 1]:
                findings.append({"slide": no, "level": "warning",
                                 "issue": "evidence quote not located verbatim on cited page"})
        # numeric fidelity on claim + bullets
        cited_pages = [int(e["page"]) for e in slide.get("evidence", []) if e.get("page")]
        haystack = " ".join(norm_pages[p - 1] for p in cited_pages if 1 <= p <= len(norm_pages))
        slide_text = " ".join([slide.get("claim", "")] + [b.get("text", "") for b in slide.get("bullets", [])])
        for m in NUMBER_RE.finditer(slide_text):
            token = normalize_text(m.group())
            if len(token) < 2 or token in haystack:
                continue
            # ignore lone 4-digit years and slide-number style tokens
            if re.fullmatch(r"(19|20)\d{2}", m.group().strip()):
                continue
            findings.append({"slide": no, "level": "warning",
                             "issue": f"number/unit '{m.group().strip()}' not found on cited page(s)"})
    return findings


def _notes_checks(plan: dict) -> list[dict]:
    out = []
    seen: dict[str, int] = {}
    for slide in plan["slides"]:
        notes = slide.get("notes", "")
        if not notes.strip():
            out.append({"slide": slide["slide_no"], "level": "warning", "issue": "missing speaker notes"})
            continue
        ratio = difflib.SequenceMatcher(None, normalize_text(notes),
                                        normalize_text(_slide_text(slide))).ratio()
        if ratio > 0.75:
            out.append({"slide": slide["slide_no"], "level": "warning",
                        "issue": f"notes duplicate slide text (similarity {ratio:.2f})"})
        key = normalize_text(notes)
        for prev_key, prev_no in seen.items():
            if difflib.SequenceMatcher(None, key, prev_key).ratio() > 0.95:
                out.append({"slide": slide["slide_no"], "level": "warning",
                            "issue": f"notes nearly identical to slide {prev_no}"})
                break
        seen[key] = slide["slide_no"]
    return out


def run(work_dir: Path) -> dict:
    plan = read_json(work_dir / SLIDE_PLAN)
    render = read_json(work_dir / RENDER_JSON)
    build_report = read_json(work_dir / "build_report.json")
    paper = read_json(work_dir / "paper.json")
    brief = read_json(work_dir / CASE_BRIEF)
    changes = ChangeLog(work_dir)

    overflow = _render_overflow(render["pdf"])
    font_warnings = _font_check(render, build_report, plan)
    pdf_password = (brief.get("constraints") or {}).get("pdf_password")
    page_text = page_texts(paper, pdf_password)
    # scanned pages: verify against OCR text instead of the missing text layer
    for pno in paper.get("scanned_pages", []):
        ocr = paper.get("ocr_texts", {}).get(str(pno), "")
        if ocr:
            page_text[pno - 1] = f"{page_text[pno - 1]} {ocr}".strip()
    evidence = _evidence_checks(plan, paper, page_text)
    notes = _notes_checks(plan)

    cap = (brief.get("constraints") or {}).get("max_slides")
    cap_error = bool(cap and len(plan["slides"]) > cap)

    schematic_assets = [
        {"slide": s["slide_no"], "figure": f["id"]}
        for s in plan["slides"] for f in s.get("figures", []) if f.get("is_schematic")
    ]
    cited_assets = [
        {"slide": s["slide_no"], "figure": f["id"]}
        for s in plan["slides"] for f in s.get("figures", []) if f.get("is_cited_work")
    ]

    errors = [e for e in evidence if e["level"] == "error"]
    if cap_error:
        errors.append({"slide": None, "level": "error", "issue": f"slide count exceeds cap {cap}"})
    for pages in overflow.values():
        errors.extend({"slide": None, "level": "error", "issue": p["type"]} for p in pages)
    warnings = [e for e in evidence if e["level"] == "warning"]
    warnings += [{"slide": None, "level": "warning", "issue": w} for w in font_warnings]
    warnings += notes
    for w in build_report.get("warnings", []):
        warnings.append({"slide": None, "level": "warning", "issue": w})

    report = {
        "passed": not errors,
        "overflow": overflow,
        "font_warnings": font_warnings,
        "evidence_findings": evidence,
        "notes_findings": notes,
        "schematic_assets": schematic_assets,
        "cited_assets": cited_assets,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "slide_count": len(plan["slides"]),
        "cap": cap,
    }
    write_json(work_dir / QA_REPORT, report)
    changes.add("qa", "check", f"errors={len(errors)} warnings={len(warnings)}")
    log.info("QA passed=%s errors=%d warnings=%d", report["passed"], len(errors), len(warnings))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    run(Path(args.work_dir))


if __name__ == "__main__":
    main()
