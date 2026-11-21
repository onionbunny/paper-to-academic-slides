"""Stage 1 — intake.

Reads case.json / task.md, inventories every supplied file, assigns roles
(paper, template, starting deck, supplementary doc, image, data) and freezes
the hard constraints (audience, minutes, slide cap, language). No model
reasoning happens here; everything is deterministic and auditable.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    CASE_BRIEF, INPUT_DIR, WORK_DIR, ChangeLog, ensure_dir,
    get_logger, read_json, read_text, write_json,
)

log = get_logger("intake")

PAPER_EXT = {".pdf"}
DECK_EXT = {".pptx", ".ppt"}
DOC_EXT = {".docx", ".doc", ".txt", ".md", ".rtf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp"}
DATA_EXT = {".csv", ".xlsx", ".xls", ".json", ".tsv"}

TEMPLATE_KEY_HINTS = ("template", "master", "theme", "母版", "模板")
START_DECK_KEY_HINTS = ("starting", "starter", "base_deck", "deck", "slides", "起始", "初始")
TEMPLATE_NAME_HINTS = ("template", "master", "theme", "blank", "母版", "模板")


def _resolve(path_value: str, input_dir: Path) -> Path:
    p = Path(path_value)
    if not p.is_absolute():
        p = input_dir / p
    return p


def _classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in PAPER_EXT:
        return "paper"
    if ext in DECK_EXT:
        return "deck"
    if ext in DOC_EXT:
        return "doc"
    if ext in IMAGE_EXT:
        return "image"
    if ext in DATA_EXT:
        return "data"
    return "other"


def _deck_role(key: str, name: str, has_slides: bool) -> str:
    hay = f"{key} {name}".lower()
    if any(h in hay for h in TEMPLATE_KEY_HINTS):
        return "template"
    if any(h in hay for h in START_DECK_KEY_HINTS):
        return "starting_deck"
    if any(h in name.lower() for h in TEMPLATE_NAME_HINTS):
        return "template"
    # A deck that already contains real slides is a starting deck; an empty
    # deck (only master/layouts) is treated as a template.
    return "starting_deck" if has_slides else "template"


def _deck_has_slides(path: Path) -> bool:
    if path.suffix.lower() != ".pptx":
        return False
    try:
        from pptx import Presentation
        return len(Presentation(str(path)).slides) > 0
    except Exception as exc:
        log.warning("cannot open deck %s: %s", path.name, exc)
        return False


def _backup_reserve(cap: int) -> int:
    if cap >= 14:
        return 2
    if cap >= 10:
        return 1
    return 0


def run(input_dir: Path | None = None, work_dir: Path | None = None) -> dict:
    input_dir = Path(input_dir or INPUT_DIR)
    work_dir = Path(work_dir or WORK_DIR)
    ensure_dir(work_dir)
    changes = ChangeLog(work_dir)

    case_path = input_dir / "case.json"
    task_path = input_dir / "task.md"
    if not case_path.exists():
        raise FileNotFoundError(f"missing required input: {case_path}")
    case = read_json(case_path)
    task = read_text(task_path) if task_path.exists() else ""

    materials = case.get("materials", {}) or {}
    declared: list[tuple[str, Path]] = []
    if isinstance(materials, dict):
        for key, value in materials.items():
            if isinstance(value, str):
                declared.append((key, _resolve(value, input_dir)))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        declared.append((key, _resolve(item, input_dir)))

    declared_set = {p.resolve() for _, p in declared if p.exists()}
    inventory: list[dict] = []
    seen: set[Path] = set()

    def register(key: str, path: Path, explicit: bool) -> None:
        if not path.exists() or path.resolve() in seen:
            return
        seen.add(path.resolve())
        kind = _classify(path)
        role = kind
        if kind == "deck":
            role = _deck_role(key, path.name, _deck_has_slides(path))
        size = path.stat().st_size
        inventory.append({
            "key": key or None,
            "file": str(path),
            "name": path.name,
            "extension": path.suffix.lower(),
            "kind": kind,
            "role": role,
            "explicit": explicit,
            "size_bytes": size,
            "exists": True,
        })
        changes.add("intake", "inventory", f"{role}: {path.name} ({size} bytes)")

    for key, path in declared:
        register(key, path, explicit=True)
    # pick up any undeclared files sitting in the input directory
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.name not in {"case.json", "task.md"}:
            if path.resolve() not in declared_set:
                register("", path, explicit=False)

    missing = [str(p) for _, p in declared if not p.exists()]
    for m in missing:
        log.warning("declared material does not exist: %s", m)

    papers = [x for x in inventory if x["role"] == "paper"]
    if not papers:
        # a PDF may have been classified generically; still flag honestly
        log.warning("no paper PDF found in input")

    cap = int(case.get("max_slides", 15))
    minutes = case.get("time_minutes")
    minutes = float(minutes) if minutes is not None else float(cap)
    audience = case.get("audience", "a general academic audience")
    language = case.get("language")  # may be unknown until extraction

    brief = {
        "case_id": case.get("case_id", "unknown-case"),
        "task": task.strip(),
        "audience": audience,
        "time_minutes": minutes,
        "max_slides": cap,
        "language_hint": language,
        "output_contract": case.get("output_contract"),
        "pacing": {
            "target_seconds_per_slide": round(minutes * 60 / max(1, cap), 1),
            "suggested_backup_reserve": _backup_reserve(cap),
            "talk_slide_target": cap - _backup_reserve(cap),
        },
        "inventory": inventory,
        "missing_materials": missing,
        "roles": {
            "papers": [x["file"] for x in inventory if x["role"] == "paper"],
            "templates": [x["file"] for x in inventory if x["role"] == "template"],
            "starting_decks": [x["file"] for x in inventory if x["role"] == "starting_deck"],
            "docs": [x["file"] for x in inventory if x["role"] == "doc"],
            "images": [x["file"] for x in inventory if x["role"] == "image"],
            "data": [x["file"] for x in inventory if x["role"] == "data"],
        },
    }
    out = write_json(work_dir / CASE_BRIEF, brief)
    log.info("case %s: %d files, cap %d slides, %.0f min, audience=%s",
             brief["case_id"], len(inventory), cap, minutes, audience)
    return {"brief": str(out)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir")
    parser.add_argument("--work-dir")
    args = parser.parse_args()
    run(args.input_dir, args.work_dir)


if __name__ == "__main__":
    main()
