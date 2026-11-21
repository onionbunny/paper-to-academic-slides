"""Stage 7 — assemble /app/output and write result.json.

Copies every reviewer-facing artifact (editable PPTX, rendered PDF, PNG
previews, page-level evidence map, plan and QA report) and writes the result
contract with honest status, sources, uncertainty, warnings/errors, change
records and the full environment/version fingerprint.
"""
from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from common import (
    CASE_BRIEF,
    CHANGE_LOG,
    OUTPUT_DIR,
    PAPER_JSON,
    QA_REPORT,
    RENDER_JSON,
    RESULT_JSON,
    SLIDE_PLAN,
    TEMPLATE_JSON,
    WORK_DIR,
    ChangeLog,
    copy_file,
    ensure_dir,
    get_logger,
    read_json,
    write_json,
)

log = get_logger("finalize")


def _evidence_map(plan: dict, paper: dict) -> list[dict]:
    fig_index = {f["id"]: f for f in paper.get("figures", [])}
    tbl_index = {t["id"]: t for t in paper.get("tables", [])}
    rows = []
    for slide in plan["slides"]:
        sources = []
        for ev in slide.get("evidence", []):
            sources.append({
                "kind": ev.get("kind"), "source_id": ev.get("id"),
                "page": ev.get("page"), "section": ev.get("section"),
                "verbatim_quote": ev.get("quote", ""),
            })
        for f in slide.get("figures", []):
            src = fig_index.get(str(f["id"]), {})
            sources.append({
                "kind": "figure", "source_id": f["id"], "page": src.get("page"),
                "section": None, "verbatim_quote": f.get("caption_verbatim", ""),
                "asset_type": src.get("asset_type"),
                "is_schematic": f.get("is_schematic"),
                "is_cited_work": f.get("is_cited_work"),
            })
        for t in slide.get("tables", []):
            src = tbl_index.get(str(t["id"]), {})
            sources.append({
                "kind": "table", "source_id": t["id"], "page": src.get("page"),
                "section": None, "verbatim_quote": src.get("caption", ""),
            })
        rows.append({
            "slide_no": slide["slide_no"],
            "role": slide["role"],
            "claim": slide.get("claim", slide.get("title", "")),
            "source_label": slide.get("source_label", ""),
            "sources": sources,
        })
    return rows


def _write_evidence_artifacts(rows: list[dict], output: Path) -> dict:
    import csv
    json_path = output / "evidence_map.json"
    csv_path = output / "evidence_map.csv"
    write_json(json_path, rows)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["slide_no", "role", "claim", "source_label", "source_count", "sources_detail"])
        for r in rows:
            detail = " | ".join(
                f"{s['kind']} {s.get('source_id') or ''} p.{s.get('page') or '?'}" for s in s_sources(r)
            )
            writer.writerow([r["slide_no"], r["role"], r["claim"], r["source_label"],
                             len(s_sources(r)), detail])
    xlsx_path = _write_xlsx(rows, output / "evidence_map.xlsx")
    return {"json": str(json_path), "csv": str(csv_path), "xlsx": str(xlsx_path)}


def s_sources(row):
    return row["sources"]


def _write_xlsx(rows: list[dict], path: Path) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except Exception:
        return Path("")
    wb = Workbook()
    ws = wb.active
    ws.title = "evidence_map"
    headers = ["slide", "role", "claim", "source_label", "kind", "source_id", "page", "verbatim"]
    ws.append(headers)
    fill = PatternFill("solid", fgColor="1F4E79")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        if not r["sources"]:
            ws.append([r["slide_no"], r["role"], r["claim"], r["source_label"], "", "", "", ""])
            continue
        for s in r["sources"]:
            ws.append([
                r["slide_no"], r["role"], r["claim"], r["source_label"],
                s.get("kind"), s.get("source_id"), s.get("page"),
                (s.get("verbatim_quote") or "")[:500],
            ])
    widths = [7, 11, 42, 18, 9, 10, 7, 70]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    wb.save(str(path))
    return path


def _environment(template: dict, render: dict) -> dict:
    import pptx
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "python_pptx": pptx.__version__,
        "soffice": render.get("soffice"),
        "template_used": template.get("source_path"),
        "slide_size_in": [template.get("slide_width"), template.get("slide_height")],
        "embedded_fonts": render.get("embedded_fonts"),
    }


def run(work_dir: Path, output_dir: Path) -> dict:
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / PAPER_JSON)
    plan = read_json(work_dir / SLIDE_PLAN)
    qa = read_json(work_dir / QA_REPORT)
    render = read_json(work_dir / RENDER_JSON)
    template = read_json(work_dir / TEMPLATE_JSON)
    changes = ChangeLog(work_dir)

    out = ensure_dir(output_dir)
    previews_out = ensure_dir(out / "previews")
    case_id = str(brief.get("case_id", "case"))
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in case_id)

    deck_name = f"{safe_id}_academic_deck.pptx"
    pdf_name = f"{safe_id}_academic_deck.pdf"
    deck_path = copy_file(work_dir / "academic_deck.pptx", out / deck_name)
    pdf_path = copy_file(render["pdf"], out / pdf_name)
    png_paths = []
    for png in render["png_previews"]:
        p = Path(png)
        png_paths.append(str(copy_file(p, previews_out / p.name)))

    evidence_rows = _evidence_map(plan, paper)
    evidence_artifacts = _write_evidence_artifacts(evidence_rows, out)
    plan_path = copy_file(work_dir / SLIDE_PLAN, out / SLIDE_PLAN)
    qa_path = copy_file(work_dir / QA_REPORT, out / QA_REPORT)
    paper_path = copy_file(work_dir / PAPER_JSON, out / PAPER_JSON)

    status = "success" if qa["passed"] and not qa["error_count"] else "partial"
    result = {
        "case_id": case_id,
        "status": status,
        "title": paper.get("title"),
        "summary": {
            "spine": plan.get("spine", {}),
            "slide_count": len(plan["slides"]),
            "cap": qa.get("cap"),
            "language": plan.get("language"),
            "audience": (brief.get("constraints") or {}).get("audience"),
            "minutes": (brief.get("constraints") or {}).get("minutes"),
        },
        "artifacts": {
            "editable_presentation": str(deck_path),
            "pdf_preview": str(pdf_path),
            "png_previews": png_paths,
            "evidence_map": evidence_artifacts,
            "slide_plan": str(plan_path),
            "qa_report": str(qa_path),
            "paper_extraction": str(paper_path),
        },
        "evidence": evidence_rows,
        "sources": brief.get("files", []),
        "uncertainty": plan.get("uncertainty", []),
        "warnings": [w.get("issue", str(w)) for w in qa.get("warnings", [])] + brief.get("warnings", []),
        "errors": [e.get("issue", str(e)) for e in qa.get("errors", [])],
        "change_records": read_json(work_dir / CHANGE_LOG, default=[]),
        "environment": _environment(template, render),
        "output_contract_version": "1.0",
    }
    # propagate any organizer-provided output contract keys without overwriting ours
    contract = (brief.get("constraints") or {}).get("output_contract")
    if isinstance(contract, dict):
        for k, v in contract.items():
            result.setdefault(k, v)

    write_json(out / RESULT_JSON, result)
    changes.add("finalize", "assemble", f"output ready at {out}")
    log.info("status=%s artifacts at %s", status, out)
    return result


def write_failure_result(work_dir: Path, output_dir: Path, errors: list[str],
                         status: str = "failed") -> dict:
    """Honest failure/partial contract when the pipeline cannot complete.
    Never fabricates deck artifacts or scientific content."""
    out = ensure_dir(output_dir)
    brief = read_json(work_dir / CASE_BRIEF, default={})
    case_id = str(brief.get("case_id", "case"))
    result = {
        "case_id": case_id,
        "status": status,
        "title": None,
        "summary": None,
        "artifacts": {},
        "evidence": [],
        "sources": brief.get("files", []),
        "uncertainty": [],
        "warnings": brief.get("warnings", []),
        "errors": errors,
        "change_records": read_json(work_dir / CHANGE_LOG, default=[]),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "output_contract_version": "1.0",
    }
    write_json(out / RESULT_JSON, result)
    log.info("status=%s result.json written at %s", status, out)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    ap.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = ap.parse_args()
    run(Path(args.work_dir), Path(args.output_dir))


if __name__ == "__main__":
    main()
