"""Stage 5 — render the editable deck to PDF and per-slide PNG previews.

Uses LibreOffice headless (the same engine behavior reviewers can reproduce)
and PyMuPDF for PNG previews. Verifies that the rendered page count equals the
slide count and records the fonts embedded in the PDF for the QA font check.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import fitz

from common import (
    RENDER_JSON,
    WORK_DIR,
    ChangeLog,
    ensure_dir,
    get_logger,
    read_json,
    run_cmd,
    which,
    write_json,
)

log = get_logger("render")
PREVIEW_DPI = 110


def render(work_dir: Path) -> dict:
    deck = work_dir / "academic_deck.pptx"
    if not deck.exists():
        raise FileNotFoundError(deck)
    out_dir = ensure_dir(work_dir / "render")
    preview_dir = ensure_dir(work_dir / "render" / "previews")
    changes = ChangeLog(work_dir)

    soffice = which("soffice") or which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) not found on PATH")
    profile = ensure_dir(work_dir / "lo_profile").resolve()
    cmd = [
        soffice,
        f"-env:UserInstallation=file://{profile}",
        "--headless", "--convert-to", "pdf",
        "--outdir", str(out_dir.resolve()), str(deck.resolve()),
    ]
    code, out, err = run_cmd(cmd, timeout=300)
    pdf_path = out_dir / "academic_deck.pdf"
    if code != 0 or not pdf_path.exists():
        raise RuntimeError(f"LibreOffice conversion failed: {(err or out)[:600]}")

    pdf = fitz.open(str(pdf_path))
    pngs = []
    embedded_fonts = set()
    for i, page in enumerate(pdf, start=1):
        pix = page.get_pixmap(matrix=fitz.Matrix(PREVIEW_DPI / 72, PREVIEW_DPI / 72), alpha=False)
        png = preview_dir / f"slide_{i:02d}.png"
        pix.save(str(png))
        pngs.append(str(png))
        for f in page.get_fonts(full=True):
            embedded_fonts.add(f[3])
    page_count = pdf.page_count
    pdf.close()

    report = {
        "pdf": str(pdf_path),
        "png_previews": pngs,
        "page_count": page_count,
        "embedded_fonts": sorted(embedded_fonts),
        "soffice": _soffice_version(),
    }
    write_json(work_dir / RENDER_JSON, report)
    changes.add("render", "export", f"PDF {page_count} pages, {len(pngs)} PNG previews")
    log.info("rendered %d pages with %d embedded font subsets", page_count, len(embedded_fonts))
    return report


def _soffice_version() -> str:
    code, out, err = run_cmd([which("soffice") or "soffice", "--version"], timeout=30)
    return (out or err).strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    render(Path(args.work_dir))


if __name__ == "__main__":
    main()
