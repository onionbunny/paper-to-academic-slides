"""Stage 1 — case intake.

Reads /input/case.json and /input/task.md, enumerates every supplied material,
classifies files (paper / template / starting deck / other) and extracts the
organizer-frozen constraints (audience, time budget, slide cap, language).
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import (
    CASE_BRIEF,
    WORK_DIR,
    ensure_dir,
    get_logger,
    read_json,
    read_text,
    write_json,
    ChangeLog,
)

log = get_logger("intake")

PAPER_EXT = {".pdf"}
DECK_EXT = {".pptx", ".ppt"}
DOC_EXT = {".docx", ".doc"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp"}

TEMPLATE_HINTS = ("template", "master", "theme", "模板", "母版", "版式")
START_HINTS = ("start", "base", "seed", "starter", "initial", "底稿", "起始", "初始")


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            files.append(p)
    return sorted(files, key=lambda x: str(x).lower())


def _classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in PAPER_EXT:
        return "paper_pdf"
    if ext in DECK_EXT:
        return "deck_pptx"
    if ext in DOC_EXT:
        return "document"
    if ext in IMG_EXT:
        return "image"
    return "other"


def _flat_values(obj, prefix=""):
    """Yield (dotted_key, value) leaves of arbitrary nested JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flat_values(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flat_values(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _extract_constraints(case: dict, task_md: str) -> dict:
    constraints = {
        "max_slides": None,
        "minutes": None,
        "audience": None,
        "language": None,
        "title": None,
        "output_contract": None,
    }
    key_patterns = {
        "max_slides": re.compile(r"(slide|page|页|幻灯)", re.I),
        "minutes": re.compile(r"(time|minute|duration|时长|分钟|时间)", re.I),
        "audience": re.compile(r"(audience|受众|听众|观众)", re.I),
        "language": re.compile(r"(lang|language|语言)", re.I),
        "title": re.compile(r"(title|名称|标题)", re.I),
    }
    for key, value in _flat_values(case):
        last = key.split(".")[-1].rstrip("0123456789[]")
        for field, pat in key_patterns.items():
            if pat.search(key) and value not in (None, ""):
                if field in ("max_slides", "minutes"):
                    m = re.search(r"\d+", str(value))
                    num = int(m.group()) if m else None
                    if num and (constraints[field] is None or num < constraints[field]):
                        constraints[field] = num
                elif constraints[field] is None:
                    constraints[field] = str(value)
        if "output_contract" in key.lower() or "contract" in key.lower():
            constraints["output_contract"] = constraints["output_contract"] or value
    # task.md fallback patterns
    if constraints["max_slides"] is None:
        m = re.search(r"(\d{1,2})\s*(?:slides?|pages?|页|张幻灯)", task_md, re.I)
        if m:
            constraints["max_slides"] = int(m.group(1))
    if constraints["minutes"] is None:
        m = re.search(r"(\d{1,2})\s*(?:minutes?|mins?|分钟|分钟汇报|分钟报告)", task_md, re.I)
        if m:
            constraints["minutes"] = int(m.group(1))
    m = re.search(r"受众[:：]\s*([^\n；;]+)", task_md)
    if m and not constraints["audience"]:
        constraints["audience"] = m.group(1).strip()
    # case-provided PDF password (never guessed or brute-forced)
    pw = case.get("pdf_password") or case.get("password")
    if not pw and isinstance(case.get("materials"), dict):
        pw = case["materials"].get("password")
    if pw:
        constraints["pdf_password"] = str(pw)
    return constraints


def _resolve_case_id(case: dict, input_dir: Path) -> str:
    for key in ("case_id", "id", "caseId", "case"):
        if isinstance(case, dict) and case.get(key):
            return str(case[key])
    return input_dir.name or "unknown_case"


def run(input_dir: Path, work_dir: Path) -> dict:
    ensure_dir(work_dir)
    changes = ChangeLog(work_dir)
    warnings: list[str] = []

    case_path = input_dir / "case.json"
    task_path = input_dir / "task.md"
    case = read_json(case_path, default={}) if case_path.exists() else {}
    if not case_path.exists():
        warnings.append("case.json not found; constraints inferred from files and task.md")
    task_md = read_text(task_path) if task_path.exists() else ""
    if not task_path.exists():
        warnings.append("task.md not found")

    files = _walk_files(input_dir)
    file_records = []
    for f in files:
        if f.name in ("case.json", "task.md"):
            continue
        kind = _classify(f)
        lower = f.name.lower()
        role_hint = None
        if kind == "deck_pptx":
            if any(h in lower for h in TEMPLATE_HINTS) or any(h in f.name for h in TEMPLATE_HINTS):
                role_hint = "template"
            elif any(h in lower for h in START_HINTS) or any(h in f.name for h in START_HINTS):
                role_hint = "starting_deck"
        file_records.append(
            {
                "path": str(f.resolve()),
                "rel": str(f.relative_to(input_dir)),
                "name": f.name,
                "ext": f.suffix.lower(),
                "size": f.stat().st_size,
                "kind": kind,
                "role_hint": role_hint,
            }
        )

    paper_pdfs = [r["path"] for r in file_records if r["kind"] == "paper_pdf"]
    decks = [r for r in file_records if r["kind"] == "deck_pptx"]
    template_deck = None
    starting_deck = None
    hinted_template = [r for r in decks if r["role_hint"] == "template"]
    hinted_start = [r for r in decks if r["role_hint"] == "starting_deck"]
    if hinted_template:
        template_deck = hinted_template[0]["path"]
    if hinted_start:
        starting_deck = hinted_start[0]["path"]
    if not template_deck and not starting_deck and decks:
        # single unlabeled deck: treat as the editable template by default
        template_deck = decks[0]["path"]
    elif len(decks) == 1 and not template_deck and starting_deck:
        template_deck = starting_deck
    if len(paper_pdfs) > 1:
        warnings.append("multiple PDFs found; the largest is treated as the primary paper")
        paper_pdfs = sorted(paper_pdfs, key=lambda p: Path(p).stat().st_size, reverse=True)

    constraints = _extract_constraints(case if isinstance(case, dict) else {}, task_md)
    if constraints["max_slides"] is None and constraints["minutes"]:
        # common academic convention: ~1 slide per minute plus title
        constraints["max_slides"] = constraints["minutes"] + 1
        warnings.append("max_slides derived from time budget (1 slide/minute + title)")

    case_id = _resolve_case_id(case if isinstance(case, dict) else {}, input_dir)
    brief = {
        "case_id": case_id,
        "input_dir": str(input_dir.resolve()),
        "files": file_records,
        "paper_pdfs": paper_pdfs,
        "primary_paper": paper_pdfs[0] if paper_pdfs else None,
        "template_pptx": template_deck,
        "starting_pptx": starting_deck,
        "constraints": constraints,
        "raw_case": case,
        "task_md": task_md,
        "warnings": warnings,
    }
    write_json(work_dir / CASE_BRIEF, brief)
    changes.add("intake", "write", f"case brief with {len(file_records)} material files")
    log.info("case_id=%s papers=%d template=%s start=%s warnings=%d",
             case_id, len(paper_pdfs), bool(template_deck), bool(starting_deck), len(warnings))
    return brief


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default=str(INPUT_DIR_DEFAULT))
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    run(Path(args.input_dir), Path(args.work_dir))


INPUT_DIR_DEFAULT = Path(__import__("common").INPUT_DIR)

if __name__ == "__main__":
    main()
