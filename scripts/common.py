"""Shared helpers for the paper-to-academic-slides skill.

All paths are absolute; the skill never assumes the current working directory.
The module is intentionally dependency-light so every stage can import it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EMU_PER_INCH = 914400
PT_PER_INCH = 72.0

# ---------------------------------------------------------------------------
# Paths with runtime defaults (evaluator contract) and overridable env vars
# ---------------------------------------------------------------------------
INPUT_DIR = Path(os.environ.get("SKILL_INPUT_DIR", "/input"))
WORK_DIR = Path(os.environ.get("SKILL_WORK_DIR", "/app/work"))
OUTPUT_DIR = Path(os.environ.get("SKILL_OUTPUT_DIR", "/app/output"))

PAPER_JSON = "paper.json"
CASE_BRIEF = "case_brief.json"
TEMPLATE_JSON = "template.json"
SLIDE_PLAN = "slide_plan.json"
QA_REPORT = "qa_report.json"
RENDER_JSON = "render.json"
RESULT_JSON = "result.json"
CHANGE_LOG = "change_records.json"


def ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSON / IO
# ---------------------------------------------------------------------------
def read_json(path: str | Path, default=None):
    path = Path(path)
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def read_text(path: str | Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return Path(path).read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return Path(path).read_text(errors="replace")


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
# Change records (audit trail of every mutating action)
# ---------------------------------------------------------------------------
class ChangeLog:
    def __init__(self, work_dir: str | Path):
        self.path = Path(work_dir) / CHANGE_LOG
        self.records = read_json(self.path, default=[])

    def add(self, stage: str, action: str, detail: str) -> None:
        self.records.append(
            {"timestamp": ts(), "stage": stage, "action": action, "detail": detail}
        )
        write_json(self.path, self.records)


# ---------------------------------------------------------------------------
# Logging that also streams to stdout for the native trace
# ---------------------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger("paper2slides")


# ---------------------------------------------------------------------------
# Subprocess helper (LibreOffice / poppler)
# ---------------------------------------------------------------------------
def run_cmd(cmd: list[str], timeout: int = 300, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def which(binary: str) -> str | None:
    return shutil.which(binary)


# ---------------------------------------------------------------------------
# Language / text helpers
# ---------------------------------------------------------------------------
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(CJK_RE.findall(text)) / max(1, len(text))


def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace and punctuation variants for matching."""
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")").replace("，", ",")
    text = text.replace("：", ":").replace("；", ";").replace("－", "-").replace("–", "-")
    return re.sub(r"\s+", "", text).lower()


def dehyphenate(text: str) -> str:
    """Join line-break hyphenation: 'informa-\\ntion' -> 'information'.
    Keep the hyphen when the next line starts with a capital (proper compound)."""
    if not text:
        return ""
    return re.sub(r"([A-Za-z])-\n([a-z])", r"\1\2", text)


def truncate_words(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    # prefer a clean break at the last sentence/phrase boundary
    for sep in ("。", ".", ";", "；", ",", "，", " "):
        pos = cut.rfind(sep)
        if pos > max_chars * 0.5:
            return cut[: pos + 1].strip()
    return cut.strip()


# ---------------------------------------------------------------------------
# Font policy: cross-platform substitution table
# ---------------------------------------------------------------------------
FONT_ALIASES = {
    "microsoft yahei": "Noto Sans CJK SC",
    "微软雅黑": "Noto Sans CJK SC",
    "pingfang sc": "Noto Sans CJK SC",
    "heiti sc": "Noto Sans CJK SC",
    "simhei": "Noto Sans CJK SC",
    "黑体": "Noto Sans CJK SC",
    "dengxian": "Noto Sans CJK SC",
    "simsun": "Noto Serif CJK SC",
    "宋体": "Noto Serif CJK SC",
    "stsong": "Noto Serif CJK SC",
    "fangsong": "Noto Serif CJK SC",
    "仿宋": "Noto Serif CJK SC",
    "kaiti": "Noto Serif CJK SC",
    "楷体": "Noto Serif CJK SC",
    "calibri": "Carlito",
    "carlito": "Carlito",
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "times new roman": "Liberation Serif",
    "cambria": "Caladea",
    "caladea": "Caladea",
    "georgia": "Liberation Serif",
}

DEFAULT_LATIN = "Carlito"
DEFAULT_LATIN_FALLBACK = "Liberation Sans"
DEFAULT_CJK_SANS = "Noto Sans CJK SC"
DEFAULT_CJK_SERIF = "Noto Serif CJK SC"


def _available_fonts() -> set[str]:
    """Return the set of font family names available on this machine."""
    cache_attr = "_font_cache"
    if hasattr(_available_fonts, cache_attr):
        return getattr(_available_fonts, cache_attr)
    names: set[str] = set()
    try:
        if sys.platform.startswith("linux") and which("fc-list"):
            code, out, _ = run_cmd(["fc-list", ":", "family"], timeout=60)
            if code == 0:
                for line in out.splitlines():
                    for part in line.split(","):
                        names.add(part.strip().lower())
        elif sys.platform == "win32":
            font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
            if font_dir.exists():
                for f in font_dir.glob("*"):
                    names.add(f.stem.lower())
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("font discovery failed: %s", exc)
    setattr(_available_fonts, cache_attr, names)
    return names


def resolve_font(preferred: str | None, cjk: bool, serif: bool = False) -> str:
    """Map a requested family to an installed, metric-compatible font."""
    available = _available_fonts()
    candidates: list[str] = []
    if preferred:
        aliased = FONT_ALIASES.get(preferred.lower().strip(), preferred.strip())
        candidates.append(aliased)
    if cjk:
        candidates.append(DEFAULT_CJK_SERIF if serif else DEFAULT_CJK_SANS)
    else:
        candidates.append(DEFAULT_LATIN)
        candidates.append(DEFAULT_LATIN_FALLBACK)
    for cand in candidates:
        if not cand:
            continue
        if not available:  # cannot verify; trust the candidate
            return cand
        if cand.lower() in available:
            return cand
        # ttc collections expose e.g. "noto sans cjk sc" exactly; also try loose match
        for known in available:
            if cand.lower() in known or known in cand.lower():
                return cand
    return candidates[-1] if candidates else (DEFAULT_CJK_SANS if cjk else DEFAULT_LATIN_FALLBACK)


# ---------------------------------------------------------------------------
# Geometry helpers (python-pptx Emu <-> inches)
# ---------------------------------------------------------------------------
def emu_to_inch(value) -> float | None:
    if value is None:
        return None
    return value / EMU_PER_INCH


def inch(value: float) -> int:
    return int(round(value * EMU_PER_INCH))


def rects_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def merge_rects(rects: list[tuple[float, float, float, float]], gap: float = 0.15) -> list[tuple[float, float, float, float]]:
    """Merge rectangles that overlap or sit within `gap` inches."""
    if not rects:
        return []
    rects = [tuple(r) for r in rects]
    merged = True
    while merged:
        merged = False
        out: list[tuple[float, float, float, float]] = []
        used = [False] * len(rects)
        for i, r in enumerate(rects):
            if used[i]:
                continue
            cur = list(r)
            for j in range(i + 1, len(rects)):
                if used[j]:
                    continue
                s = rects[j]
                expanded = (cur[0] - gap, cur[1] - gap, cur[2] + gap, cur[3] + gap)
                if rects_overlap(expanded, s):
                    cur = [min(cur[0], s[0]), min(cur[1], s[1]), max(cur[2], s[2]), max(cur[3], s[3])]
                    used[j] = True
                    merged = True
            used[i] = True
            out.append(tuple(cur))
        rects = out
    return rects
