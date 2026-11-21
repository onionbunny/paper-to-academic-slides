"""Stage 3 — template / starting-deck inspection.

Analyzes the real master/layout-based PPTX so planning respects the frozen
design: slide size, every layout and its placeholders (geometry + type),
theme colors and fonts, existing slides (starting deck), and the usable safe
area. Also renders the supplied deck to PDF/PNG thumbnails for visual checks.
When no deck is supplied, emits a neutral default-theme specification instead.
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE  # noqa: F401
from pptx.util import Inches

from common import (
    CASE_BRIEF,
    TEMPLATE_JSON,
    WORK_DIR,
    ChangeLog,
    emu_to_inch,
    ensure_dir,
    get_logger,
    read_json,
    run_cmd,
    which,
    write_json,
)

log = get_logger("template")
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def _theme_from_pptx(path: Path) -> dict:
    theme = {"colors": {}, "major_font": None, "minor_font": None, "major_ea_font": None, "minor_ea_font": None}
    try:
        with zipfile.ZipFile(path) as zf:
            theme_names = [n for n in zf.namelist() if n.startswith("ppt/theme/") and n.endswith(".xml")]
            if not theme_names:
                return theme
            root = ET.fromstring(zf.read(sorted(theme_names)[0]))
    except Exception as exc:
        log.warning("theme parse failed: %s", exc)
        return theme
    scheme = root.find(".//a:clrScheme", NS)
    if scheme is not None:
        for child in list(scheme):
            tag = child.tag.split("}")[1]
            srgb = child.find("a:srgbClr", NS)
            sysclr = child.find("a:sysClr", NS)
            if srgb is not None:
                theme["colors"][tag] = f"#{srgb.get('val')}"
            elif sysclr is not None:
                theme["colors"][tag] = f"#{sysclr.get('lastClr')}"
    font_scheme = root.find(".//a:fontScheme", NS)
    if font_scheme is not None:
        for role, key, ea_key in (("majorFont", "major_font", "major_ea_font"),
                                  ("minorFont", "minor_font", "minor_ea_font")):
            node = font_scheme.find(f"a:{role}", NS)
            if node is not None:
                latin = node.find("a:latin", NS)
                if latin is not None:
                    theme[key] = latin.get("typeface")
                ea = node.find("a:ea", NS)
                if ea is not None and ea.get("typeface"):
                    theme[ea_key] = ea.get("typeface")
    return theme


def _placeholder_records(placeholders, master_map: dict) -> list[dict]:
    out = []
    for ph in placeholders:
        ph_idx = ph.placeholder_format.idx
        left = emu_to_inch(ph.left)
        top = emu_to_inch(ph.top)
        width = emu_to_inch(ph.width)
        height = emu_to_inch(ph.height)
        if left is None and ph_idx in master_map:
            left, top, width, height = master_map[ph_idx]
        out.append(
            {
                "idx": ph_idx,
                "type": str(ph.placeholder_format.type),
                "name": ph.name,
                "left": round(left, 3) if left is not None else None,
                "top": round(top, 3) if top is not None else None,
                "width": round(width, 3) if width is not None else None,
                "height": round(height, 3) if height is not None else None,
            }
        )
    return out


def _master_placeholder_map(master) -> dict:
    m = {}
    for ph in master.placeholders:
        m[ph.placeholder_format.idx] = (
            emu_to_inch(ph.left),
            emu_to_inch(ph.top),
            emu_to_inch(ph.width),
            emu_to_inch(ph.height),
        )
    return m


def _classify_layout(placeholders: list[dict]) -> list[str]:
    types = " ".join(p["type"] for p in placeholders).upper()
    roles = []
    if "TITLE" in types and ("SUBTITLE" in types or "CENTER_TITLE" in types):
        roles.append("title")
    if "BODY" in types or "OBJECT" in types or "CONTENT" in types:
        roles.append("content")
    if "PICTURE" in types:
        roles.append("figure")
    if "TITLE" in types and not roles:
        roles.append("section")
    if not placeholders:
        roles.append("blank")
    return roles or ["generic"]


def _safe_area(layouts: list[dict], slide_w: float, slide_h: float) -> dict:
    geos = []
    for lay in layouts:
        for ph in lay["placeholders"]:
            if ph["left"] is None:
                continue
            geos.append((ph["left"], ph["top"], ph["left"] + ph["width"], ph["top"] + ph["height"]))
    if not geos:
        return {"left": 0.5, "top": 0.5, "width": slide_w - 1.0, "height": slide_h - 1.0,
                "right": slide_w - 0.5, "bottom": slide_h - 0.5}
    left = min(g[0] for g in geos)
    top = min(g[1] for g in geos)
    right = max(g[2] for g in geos)
    bottom = max(g[3] for g in geos)
    # tighten to plausible margins and keep inside slide
    left = max(0.25, min(left, 0.75))
    top = max(0.25, min(top, 0.75))
    right = min(slide_w - 0.25, max(right, slide_w - 0.75))
    bottom = min(slide_h - 0.25, max(bottom, slide_h - 0.75))
    return {"left": round(left, 3), "top": round(top, 3), "right": round(right, 3),
            "bottom": round(bottom, 3), "width": round(right - left, 3),
            "height": round(bottom - top, 3)}


def _render_deck(path: Path, work_dir: Path) -> dict:
    thumbs = ensure_dir(work_dir / "assets" / "template_thumbs")
    soffice = which("soffice") or which("libreoffice")
    if not soffice:
        return {"rendered": False, "reason": "soffice not found"}
    profile = ensure_dir(work_dir / "lo_profile_tpl").resolve()
    cmd = [soffice, f"-env:UserInstallation=file://{profile}", "--headless",
           "--convert-to", "pdf", "--outdir", str(thumbs.resolve()), str(path)]
    code, out, err = run_cmd(cmd, timeout=240)
    pdfs = list(thumbs.glob("*.pdf"))
    if code != 0 or not pdfs:
        return {"rendered": False, "reason": (err or out).strip()[:500]}
    pdf = pdfs[0]
    code, out, err = run_cmd(["pdftoppm", "-png", "-r", "72", str(pdf), str(thumbs / "thumb")], timeout=180)
    pngs = sorted(str(p) for p in thumbs.glob("thumb*.png"))
    return {"rendered": True, "pdf": str(pdf), "thumbnails": pngs}


def inspect(path: Path | None, work_dir: Path, slide_w_default: float = 13.333,
            render: bool = True) -> dict:
    if path is None:
        # analyze the exact python-pptx default deck that build() will use
        default_deck = ensure_dir(work_dir / "assets") / "default_template.pptx"
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        prs.save(str(default_deck))
        spec = inspect(default_deck, work_dir, render=False)
        spec["provided"] = False
        spec["default_theme"] = True
        spec["render"] = {"rendered": False, "reason": "no template supplied; default theme used"}
        write_json(work_dir / TEMPLATE_JSON, spec)
        return spec

    prs = Presentation(str(path))
    slide_w = prs.slide_width / 914400
    slide_h = prs.slide_height / 914400
    theme = _theme_from_pptx(path)
    layouts = []
    for master in prs.slide_masters:
        master_map = _master_placeholder_map(master)
        for layout in master.slide_layouts:
            placeholders = _placeholder_records(layout.placeholders, master_map)
            layouts.append(
                {
                    "name": layout.name,
                    "master": master.name if hasattr(master, "name") else "master",
                    "placeholders": placeholders,
                    "roles": _classify_layout(placeholders),
                }
            )
    existing = []
    for i, slide in enumerate(prs.slides, start=1):
        existing.append(
            {
                "index": i,
                "layout": slide.slide_layout.name,
                "shape_count": len(slide.shapes),
                "texts": [s.text_frame.text[:120] for s in slide.shapes
                          if s.has_text_frame and s.text_frame.text.strip()][:5],
            }
        )
    spec = {
        "provided": True,
        "source_path": str(path.resolve()),
        "slide_width": round(slide_w, 3),
        "slide_height": round(slide_h, 3),
        "theme": theme,
        "layouts": layouts,
        "existing_slides": existing,
        "safe_area": _safe_area(layouts, slide_w, slide_h),
        "render": _render_deck(path, work_dir) if render else
        {"rendered": False, "reason": "render skipped"},
    }
    write_json(work_dir / TEMPLATE_JSON, spec)
    log.info("template %s: %d layouts, %d existing slides, rendered=%s",
             path.name, len(layouts), len(existing), spec["render"]["rendered"])
    return spec


def run(work_dir: Path) -> dict:
    brief = read_json(work_dir / CASE_BRIEF)
    changes = ChangeLog(work_dir)
    deck = brief.get("template_pptx") or brief.get("starting_pptx")
    spec = inspect(Path(deck) if deck else None, work_dir)
    changes.add("template", "inspect", f"deck={deck or 'none'}")
    return spec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    run(Path(args.work_dir))


if __name__ == "__main__":
    main()
