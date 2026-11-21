"""End-to-end orchestrator for the academic-slide-organizer skill.

Subcommands
  prepare  intake + extraction + template (deterministic setup)
  fallback write the deterministic fallback plan when no semantic plan exists
  validate validate slide_plan.json against the JSON schema
  build    build the deck, render previews, run QA, finalize result.json
  all      prepare -> (fallback if no semantic plan) -> build

The intended agent flow is `prepare`, then write a semantic slide_plan.json
following references/planning_prompt.md, then `build`. `all` guarantees a
usable deck even without semantic planning.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import (
    SLIDE_PLAN, ChangeLog, get_logger, read_json, write_json,
)

LOG = get_logger("run")
SCRIPTS_DIR = Path(__file__).resolve().parent
SUBMISSION_ROOT = SCRIPTS_DIR.parent
SCHEMA_PATH = SUBMISSION_ROOT / "schemas" / "slide_plan.schema.json"


def _import(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_prepare(input_dir: Path, work_dir: Path, output_dir: Path) -> None:
    _import("stage_intake").run(input_dir, work_dir)
    _import("stage_extract").run(work_dir)
    _import("stage_template").run(work_dir)
    LOG.info("prepare complete: write %s then run build", SLIDE_PLAN)


def cmd_fallback(work_dir: Path) -> None:
    if (work_dir / SLIDE_PLAN).exists():
        LOG.info("slide plan already present; fallback skipped")
        return
    _import("stage_autoplan").run(work_dir)


def cmd_validate(work_dir: Path) -> bool:
    schema = read_json(SCHEMA_PATH)
    plan = read_json(work_dir / SLIDE_PLAN)
    if plan is None:
        LOG.error("no %s in %s", SLIDE_PLAN, work_dir)
        return False
    import jsonschema
    try:
        jsonschema.validate(plan, schema)
        LOG.info("slide plan validates against schema")
        return True
    except jsonschema.ValidationError as exc:
        LOG.error("schema validation error: %s", exc.message)
        return False


def cmd_build(work_dir: Path, output_dir: Path, started_at: float) -> None:
    if not cmd_validate(work_dir):
        raise SystemExit("invalid slide plan; fix it or run fallback")
    _import("stage_build").run(work_dir, output_dir)
    _import("stage_render").run(work_dir, output_dir)
    _import("stage_qa").run(work_dir, output_dir)
    _import("stage_finalize").run(work_dir, output_dir, started_at=started_at)


def main() -> None:
    parser = argparse.ArgumentParser(description="Academic slide organizer runner")
    parser.add_argument("command", choices=["prepare", "fallback", "validate", "build", "all"])
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    from common import INPUT_DIR, WORK_DIR, OUTPUT_DIR
    input_dir = Path(args.input_dir) if args.input_dir else INPUT_DIR
    work_dir = Path(args.work_dir) if args.work_dir else WORK_DIR
    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    if args.command == "prepare":
        cmd_prepare(input_dir, work_dir, output_dir)
    elif args.command == "fallback":
        cmd_fallback(work_dir)
    elif args.command == "validate":
        raise SystemExit(0 if cmd_validate(work_dir) else 1)
    elif args.command == "build":
        cmd_build(work_dir, output_dir, started)
    elif args.command == "all":
        cmd_prepare(input_dir, work_dir, output_dir)
        cmd_fallback(work_dir)
        cmd_build(work_dir, output_dir, started)


if __name__ == "__main__":
    main()
