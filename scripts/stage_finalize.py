"""Finalize the run: evidence ledger, notes file and result.json."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from common import (
    CHANGE_RECORDS, QA_REPORT, CASE_BRIEF, PAPER_JSON, RENDER_JSON, SLIDE_PLAN,
    TEMPLATE_JSON, get_logger, read_json, write_json,
)

LOG = get_logger("finalize")
SKILL_NAME = "academic-slide-organizer"
SKILL_VERSION = "1.0.0"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_evidence_map(plan, qa, output_dir: Path) -> str:
    verdict_by_no = {v["slide_no"]: v["verbatim_check"] for v in qa.get("verdicts", [])}
    lines = ["# Evidence map", "",
             "Every slide's claims are bound to real source locations. Verdicts come from QA.", ""]
    for spec in plan["slides"]:
        tag = " (backup)" if spec.get("backup") else ""
        lines.append(f"## Slide {spec['slide_no']}{tag} — {spec['role']}")
        lines.append(f"- Title: {spec['title']}")
        lines.append(f"- Evidence strength: {spec['evidence_strength']}")
        for ev in spec.get("evidence", []):
            ref = ev.get("id") or ev.get("section") or ev.get("kind")
            label = ev["kind"] if ref == ev["kind"] else f"{ev['kind']} {ref}"
            lines.append(f"- Source: {label} · p.{ev['page']}")
            if ev.get("quote"):
                lines.append(f"  - Quote: \"{ev['quote']}\"")
        lines.append(f"- Verdict: {verdict_by_no.get(spec['slide_no'], 'flagged')}")
        lines.append("")
    path = output_dir / "evidence_map.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return "evidence_map.md"


def _write_notes(plan, output_dir: Path) -> str:
    lines = ["# Speaker notes", ""]
    for spec in plan["slides"]:
        if spec.get("backup"):
            continue
        lines.append(f"## Slide {spec['slide_no']} — {spec['title']}")
        lines.append(f"[{spec.get('timing_seconds', 0)}s]")
        lines.append(spec.get("notes", ""))
        lines.append("")
    path = output_dir / "speaker_notes.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return "speaker_notes.md"


def _sources(brief) -> list[dict]:
    out = []
    for item in brief.get("inventory", []):
        p = Path(item["file"])
        if not item.get("exists") or not p.exists():
            continue
        try:
            digest = _sha256(p)
        except Exception:
            digest = ""
        pages = 0
        if item.get("extension") == ".pdf" or p.suffix.lower() == ".pdf":
            try:
                import fitz
                pages = fitz.open(str(p)).page_count
            except Exception:
                pages = 0
        out.append({
            "path": item["file"], "role": item.get("role", "other"),
            "kind": item.get("kind", "other"), "sha256": digest, "pages": pages,
        })
    return out


def run(work_dir=None, output_dir=None, started_at: float | None = None):
    from common import OUTPUT_DIR, WORK_DIR
    work_dir = Path(work_dir or WORK_DIR)
    output_dir = Path(output_dir or OUTPUT_DIR)
    brief = read_json(work_dir / CASE_BRIEF)
    plan = read_json(work_dir / SLIDE_PLAN)
    qa = read_json(output_dir / QA_REPORT, default={})
    render = read_json(output_dir / RENDER_JSON, default={})
    tpl = read_json(work_dir / TEMPLATE_JSON)
    paper = read_json(work_dir / PAPER_JSON)

    evidence_file = _write_evidence_map(plan, qa, output_dir)
    notes_file = _write_notes(plan, output_dir)

    deck_exists = (output_dir / "deck.pptx").exists()
    if not deck_exists:
        status = "blocked"
    elif qa.get("findings") or not render.get("ok"):
        status = "partial"
    else:
        status = "complete"

    now = time.time()
    talk = [s for s in plan["slides"] if not s.get("backup")]
    result = {
        "case_id": brief["case_id"],
        "skill": {
            "name": SKILL_NAME, "version": SKILL_VERSION,
            "planner": plan.get("planner"), "model_used": None,
            "started_at": _iso(started_at or now), "finished_at": _iso(now),
            "duration_seconds": round(now - started_at, 2) if started_at else 0,
        },
        "status": status,
        "status_detail": ("; ".join(qa.get("findings", [])[:6]) or
                          ("all checks passed" if status == "complete" else "degraded run")),
        "constraints": {
            "audience": brief["audience"],
            "audience_profile": plan["constraints"]["audience_profile"],
            "time_minutes": brief["time_minutes"], "max_slides": brief["max_slides"],
            "talk_slides": len(talk),
            "backup_slides": len(plan["slides"]) - len(talk),
        },
        "artifacts": {
            "deck_pptx": "deck.pptx" if deck_exists else None,
            "deck_pdf": render.get("pdf"),
            "previews_dir": "previews",
            "preview_pngs": render.get("previews", []),
            "slide_plan": SLIDE_PLAN,
            "evidence_map": evidence_file,
            "speaker_notes": notes_file,
        },
        "evidence_map": [
            {
                "slide_no": spec["slide_no"], "backup": bool(spec.get("backup")),
                "role": spec["role"], "title": spec["title"],
                "evidence": spec.get("evidence", []),
                "verbatim_check": next((v["verbatim_check"] for v in qa.get("verdicts", [])
                                        if v["slide_no"] == spec["slide_no"]), "flagged"),
            }
            for spec in plan["slides"]
        ],
        "qa_prep": [
            {
                "category": item["category"], "question": item["question"],
                "answer_points": item["answer_points"],
                "backup_slide_no": item.get("backup_slide_no"),
            }
            for item in plan.get("qa_prep", [])
        ],
        "sources": _sources(brief),
        "rendering": {
            "engine": render.get("engine", "libreoffice+pymupdf"),
            "fonts": sorted(set(tpl.get("fonts", {}).values())),
            "theme": tpl.get("name", "scholar-default"),
            "qa_findings": qa.get("findings", []),
        },
        "uncertainty": plan.get("uncertainty", []),
        "change_records": read_json(work_dir / CHANGE_RECORDS, default=[]) or [],
        "limitations": plan.get("spine", {}).get("limitations", [])
        + plan.get("warnings", []),
    }
    write_json(output_dir / "result.json", result)
    LOG.info("finalized: status=%s, %d slides", status, len(plan["slides"]))
    return result


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat()


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    run(args[0] if args else None, args[1] if len(args) > 1 else None)
