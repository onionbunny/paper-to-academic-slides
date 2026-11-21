"""Shared helpers for the academic-deck organizer skill.

All paths are resolvable at runtime: the harness uses /input, /app/work and
/app/output, while local testing can override them with CLI flags or the
SKILL_INPUT_DIR / SKILL_WORK_DIR / SKILL_OUTPUT_DIR environment variables.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolved directories
# ---------------------------------------------------------------------------
INPUT_DIR = Path(os.environ.get("SKILL_INPUT_DIR", "/input"))
WORK_DIR = Path(os.environ.get("SKILL_WORK_DIR", "/app/work"))
OUTPUT_DIR = Path(os.environ.get("SKILL_OUTPUT_DIR", "/app/output"))

# Canonical file names inside the work directory
CASE_BRIEF = "case_brief.json"
PAPER_JSON = "paper.json"
PAPER_REVIEW = "paper_review.md"
TEMPLATE_JSON = "template.json"
SLIDE_PLAN = "slide_plan.json"
BUILD_REPORT = "build_report.json"
RENDER_JSON = "render.json"
QA_REPORT = "qa_report.json"
CHANGE_RECORDS = "change_records.json"
PLAN_LOG = "plan_log.json"

ASSETS_DIR = "assets"
RENDER_DIR = "render"
TPL_THUMBS_DIR = "template_thumbs"


def ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------
def read_json(path: str | Path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return p


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def copy_file(src: str | Path, dst: str | Path) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


# ---------------------------------------------------------------------------
# Change log (every mutation is recorded so the run can be re-audited)
# ---------------------------------------------------------------------------
class ChangeLog:
    def __init__(self, work_dir: str | Path):
        self.path = Path(work_dir) / CHANGE_RECORDS
        self.records = read_json(self.path, default=[]) or []

    def add(self, stage: str, action: str, detail: str) -> None:
        self.records.append({"time": ts(), "stage": stage, "action": action, "detail": detail})
        write_json(self.path, self.records)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def run_cmd(cmd: list[str], timeout: int = 300, cwd: str | Path | None = None,
            env: dict | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, timeout=timeout, cwd=str(cwd) if cwd else None,
            env=env, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def which(binary: str) -> str | None:
    return shutil.which(binary)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / max(1, len(text))


_WS_RE = re.compile(r"\s+")
# hyphen followed by any whitespace: covers "wide-\nspread" and "wide- spread"
_HYPHEN_RE = re.compile(r"(\w)-\s+(\w)")


def normalize_text(text: str) -> str:
    """Collapse whitespace and line-break hyphenation for verbatim checks."""
    if not text:
        return ""
    prev = None
    while prev != text:  # repeated substitution handles chained breaks
        prev = text
        text = _HYPHEN_RE.sub(r"\1\2", text)
    text = text.replace("\n", " ").replace("\r", " ")
    return _WS_RE.sub(" ", text).strip()


def dehyphenate(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _HYPHEN_RE.sub(r"\1\2", text)
    return text


def truncate_words(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut + " …"


# ---------------------------------------------------------------------------
# Font resolution
# ---------------------------------------------------------------------------
_FONT_CACHE: set[str] | None = None


def _available_fonts() -> set[str]:
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    fonts: set[str] = set()
    if which("fc-list"):
        code, out, _ = run_cmd(["fc-list", ":", "family"], timeout=60)
        if code == 0:
            for line in out.splitlines():
                for name in line.split(","):
                    fonts.add(name.strip())
    _FONT_CACHE = fonts
    return fonts


def resolve_font(preferred: str | None, cjk: bool, serif: bool = False) -> str:
    """Return a font family that is actually installed; never assume one."""
    available = _available_fonts()
    if preferred and preferred in available:
        return preferred
    if cjk:
        candidates = ["Noto Sans CJK SC", "Noto Serif CJK SC", "WenQuanYi Zen Hei",
                      "Source Han Sans SC", "SimHei"]
        if serif:
            candidates = ["Noto Serif CJK SC", "Noto Sans CJK SC", "Source Han Serif SC"]
    else:
        candidates = (["Liberation Serif", "DejaVu Serif", "Noto Serif"]
                      if serif else
                      ["Liberation Sans", "DejaVu Sans", "Noto Sans", "Arial"])
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


# ---------------------------------------------------------------------------
# Geometry helpers (PDF points)
# ---------------------------------------------------------------------------
def rects_overlap(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def rect_area(r: tuple[float, float, float, float]) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def merge_rects(rects: list[tuple[float, float, float, float]],
                gap: float = 0.15) -> list[tuple[float, float, float, float]]:
    """Union rects that overlap or sit within `gap` points (single pass clusters)."""
    clusters: list[list[float]] = []
    for r in rects:
        placed = False
        for c in clusters:
            expanded = (c[0] - gap, c[1] - gap, c[2] + gap, c[3] + gap)
            if rects_overlap(expanded, r):
                c[0] = min(c[0], r[0]); c[1] = min(c[1], r[1])
                c[2] = max(c[2], r[2]); c[3] = max(c[3], r[3])
                placed = True
                break
        if not placed:
            clusters.append([r[0], r[1], r[2], r[3]])
    # repeat until stable (new merges may become possible)
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if rects_overlap((a[0] - gap, a[1] - gap, a[2] + gap, a[3] + gap), b):
                    a[0] = min(a[0], b[0]); a[1] = min(a[1], b[1])
                    a[2] = max(a[2], b[2]); a[3] = max(a[3], b[3])
                    clusters.pop(j)
                    changed = True
                    break
            if changed:
                break
    return [tuple(c) for c in clusters]


def cluster_rects(rects, gap: float = 6.0):
    """Alias kept for call sites that think in clustering terms."""
    return merge_rects(rects, gap=gap)


def env_fingerprint() -> dict:
    import platform
    info = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        import pptx, fitz, pdfplumber, PIL
        info["lib_versions"] = {
            "python_pptx": pptx.__version__, "pymupdf": fitz.VersionBind,
            "pdfplumber": pdfplumber.__version__, "pillow": PIL.__version__,
        }
    except Exception as exc:  # pragma: no cover
        info["lib_versions_error"] = str(exc)
    if which("soffice"):
        code, out, _ = run_cmd(["soffice", "--version"], timeout=30)
        if code == 0:
            info["libreoffice"] = out.strip()
    return info


def sleep_or_return(seconds: float) -> None:
    time.sleep(seconds)
