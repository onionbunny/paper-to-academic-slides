"""Render the editable deck to a real PDF and per-slide PNG previews.

The PDF is produced by LibreOffice headless; PNGs are rendered from that
PDF with PyMuPDF so they always match the exported deck. Failures are
retried once with an isolated profile and then reported honestly.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from common import (
    CHANGE_RECORDS, RENDER_JSON, ChangeLog, get_logger, read_json, run_cmd,
    which, write_json,
)

LOG = get_logger("render")
PREVIEWS_DIR = "previews"


def _soffice_pdf(deck: Path, out_dir: Path) -> tuple[bool, str]:
    if not which("soffice"):
        return False, "soffice not installed"
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory() as profile:
            cmd = ["soffice", "--headless", f"-env:UserInstallation=file://{profile}",
                   "--convert-to", "pdf", "--outdir", str(out_dir), str(deck)]
            code, out, err = run_cmd(cmd, timeout=240)
        pdf = out_dir / "deck.pdf"
        if code == 0 and pdf.exists():
            return True, out + err
        if attempt == 2:
            return False, (out + err).strip()[:500]
    return False, "unknown render failure"


def _png_previews(pdf_path: Path, out_dir: Path) -> list[str]:
    import fitz
    previews = out_dir / PREVIEWS_DIR
    previews.mkdir(parents=True, exist_ok=True)
    pngs = []
    doc = fitz.open(str(pdf_path))
    zoom = 150 / 72
    matrix = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        name = f"slide_{i + 1:02d}.png"
        pix.save(str(previews / name))
        pngs.append(f"{PREVIEWS_DIR}/{name}")
    doc.close()
    return pngs


def run(work_dir=None, output_dir=None):
    from common import OUTPUT_DIR, WORK_DIR
    work_dir = Path(work_dir or WORK_DIR)
    output_dir = Path(output_dir or OUTPUT_DIR)
    deck = output_dir / "deck.pptx"
    changelog = ChangeLog(work_dir)

    if not deck.exists():
        report = {"pdf": None, "previews": [], "ok": False, "error": "deck.pptx missing"}
        write_json(output_dir / RENDER_JSON, report)
        return report

    ok, detail = _soffice_pdf(deck, output_dir)
    pdf_path = output_dir / "deck.pdf"
    pngs = []
    if ok:
        try:
            pngs = _png_previews(pdf_path, output_dir)
        except Exception as exc:
            detail = f"PNG rendering error: {exc}"
            ok = False
    report = {
        "pdf": "deck.pdf" if pdf_path.exists() else None,
        "previews": pngs, "ok": ok and bool(pngs),
        "pages": len(pngs), "engine": "libreoffice+pymupdf", "error": None if ok else detail,
    }
    write_json(output_dir / RENDER_JSON, report)
    changelog.add("render", "export", "pdf+png" if report["ok"] else f"failed: {detail}")
    LOG.info("render %s: %d previews", "ok" if report["ok"] else "FAILED", len(pngs))
    return report


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    run(args[0] if args else None, args[1] if len(args) > 1 else None)
