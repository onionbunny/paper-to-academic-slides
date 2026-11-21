"""Orchestrator for the paper-to-academic-slides skill.

Usage:
  python run.py prepare   # intake + PDF extraction + template analysis
  python run.py validate  # validate the agent-authored slide_plan.json
  python run.py build     # build deck + render PDF/PNG + QA
  python run.py finalize  # assemble /app/output and result.json
  python run.py auto      # full deterministic run (fallback planner)
  python run.py all       # prepare; uses existing plan, else fallback plan; then build/finalize

Paths default to the evaluator contract (/input, /app/work, /app/output) and
can be overridden with --input-dir/--work-dir/--output-dir.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage_intake
import stage_extract
import stage_template
import stage_autoplan
import stage_plan_tools
import stage_build
import stage_render
import stage_qa
import stage_finalize
from common import SLIDE_PLAN, get_logger

log = get_logger("run")


def prepare(input_dir: Path, work_dir: Path) -> None:
    stage_intake.run(input_dir, work_dir)
    stage_extract.run(work_dir)
    stage_template.run(work_dir)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["prepare", "validate", "build", "finalize", "auto", "all"])
    ap.add_argument("--input-dir", default=None)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--output-dir", default=None)
    args = ap.parse_args()

    from common import INPUT_DIR, WORK_DIR, OUTPUT_DIR
    input_dir = Path(args.input_dir or INPUT_DIR)
    work_dir = Path(args.work_dir or WORK_DIR)
    output_dir = Path(args.output_dir or OUTPUT_DIR)

    if args.command == "prepare":
        prepare(input_dir, work_dir)
        return
    if args.command == "validate":
        report = stage_plan_tools.validate(work_dir)
        if report["errors"]:
            raise SystemExit(1)
        return
    if args.command == "build":
        stage_build.build(work_dir)
        stage_render.render(work_dir)
        stage_qa.run(work_dir)
        return
    if args.command == "finalize":
        stage_finalize.run(work_dir, output_dir)
        return
    if args.command == "auto":
        try:
            prepare(input_dir, work_dir)
            stage_autoplan.build_plan(work_dir)
            stage_build.build(work_dir)
            stage_render.render(work_dir)
            stage_qa.run(work_dir)
            stage_finalize.run(work_dir, output_dir)
        except Exception as exc:
            log.error("pipeline aborted: %s", exc)
            stage_finalize.write_failure_result(work_dir, output_dir, [f"{type(exc).__name__}: {exc}"])
        return
    # all: prepare, then semantic plan if present, otherwise deterministic fallback
    try:
        prepare(input_dir, work_dir)
        if not (work_dir / SLIDE_PLAN).exists():
            log.info("no slide_plan.json found; using deterministic fallback planner")
            stage_autoplan.build_plan(work_dir)
        else:
            stage_plan_tools.validate(work_dir)
        stage_build.build(work_dir)
        stage_render.render(work_dir)
        stage_qa.run(work_dir)
        stage_finalize.run(work_dir, output_dir)
    except Exception as exc:
        log.error("pipeline aborted: %s", exc)
        stage_finalize.write_failure_result(work_dir, output_dir, [f"{type(exc).__name__}: {exc}"])


if __name__ == "__main__":
    main()
