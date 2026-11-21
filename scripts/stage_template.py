"""Stage 3 — template / starting-deck inspection.

When the case supplies a real master/layout-based template or a starting deck,
we inspect its slide size, layouts and placeholder geometry, theme colors and
typefaces, existing slides and the safe content area, and render thumbnails.
When nothing is supplied we emit a deterministic, self-contained default
theme spec (the build stage draws it programmatically — no binary assets).
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import (
    CASE_BRIEF, TEMPLATE_JSON, TPL_THUMBS_DIR, WORK_DIR, ChangeLog,
    ensure_dir, get_logger, read_json, resolve_font, run_cmd, write_json,
)

log = get_logger("template")

# Default visual identity (used only when the case gives no deck). Calm,
# scholarly palette; never neon/purple-heavy.
DEFAULT_THEME = {
    "name": "scholar-default",
    "slide_w_in": 13.333,
    "slide_h_in": 7.5,
    "colors": {
        "ink": "1F2A37",        # near-black slate for body text
        "primary": "173F5F",    # deep navy
        "accent": "2E86AB",     # steel blue
        "accent2": "E07A5F",    # muted terracotta (highlights / warnings)
        "soft": "F4F6F8",       # panel fill
        "rule": "D9DEE4",       # hairlines
        "muted": "6B7785",      # captions / footers
    },
    "safe_area": {"left": 0.62, "top": 0.52, "right": 12.72, "bottom": 6.92},
}


def _placeholder_records(placeholders) -> list[dict]:
    out = []
    for ph in placeholders:
        try:
            geom = {
                "left": ph.left / 914400, "top": ph.top / 914400,
                "width": ph.width / 914400, "height": ph.height / 914400,
            }
        except Exception:
            geom = None
        out.append({
            "idx": ph.placeholder_format.idx,
            "type": str(ph.placeholder_format.type),
            "name": ph.name, "geom": geom,
        })
    return out


def _classify_layout(placeholders: list[dict]) -> list[str]:
    types = " ".join(p["type"] for p in placeholders).lower()
    # OBJECT (generic content placeholder, type 7) is body/content, not a title
    has_body = any(k in types for k in ("body", "object", "content", "picture", "table", "chart"))
    has_title = "title" in types
    roles = []
    if has_title and has_body:
        roles.append("title_content")
        roles.append("title_body")
    if has_title and not has_body:
        roles.append("title_only")
    if "picture" in types:
        roles.append("picture")
    if "table" in types:
        roles.append("table")
    if "blank" in types or not placeholders:
        roles.append("blank")
    return roles or ["custom"]


def _theme_info(master) -> dict:
    info: dict = {}
    try:
        theme_part = master.part.related_part(
            master.part._element.map["http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"]
            if hasattr(master.part._element, "map") else None)
    except Exception:
        theme_part = None
    # robust fallback: walk the package for the theme part
    if theme_part is None:
        try:
            for rel in master.part.rels.values():
                if "theme" in rel.reltype:
                    theme_part = rel.target_part
                    break
        except Exception:
            theme_part = None
    if theme_part is not None:
        xml = theme_part.blob.decode("utf-8", errors="replace")
        colors = {}
        # parse only inside the color scheme, not the outer theme wrappers
        cs = re.search(r"<a:clrScheme.*?</a:clrScheme>", xml, re.S)
        scope = cs.group(0) if cs else xml
        # each scheme color element contains either an srgbClr or a sysClr
        for m in re.finditer(r"<a:(\w+)>(.*?)</a:\1>", scope, re.S):
            name, body = m.group(1), m.group(2)
            cm = re.search(r'srgbClr val="([0-9A-Fa-f]{6})"', body) or \
                re.search(r'sysClr[^>]*lastClr="([0-9A-Fa-f]{6})"', body)
            if cm:
                colors[name] = cm.group(1)
        info["colors"] = {k: v for k, v in list(colors.items())[:12]}
        fonts = re.findall(r"<a:latin typeface=\"([^\"]+)\"", xml)
        ea = re.findall(r"<a:ea typeface=\"([^\"]+)\"", xml)
        if fonts:
            info["latin_font"] = fonts[0]
        if ea and ea[0]:
            info["ea_font"] = ea[0]
    return info


def _safe_area(layouts: list[dict], w: float, h: float) -> dict:
    left, top, right, bottom = 0.5, 0.4, w - 0.5, h - 0.4
    for layout in layouts:
        for ph in layout["placeholders"]:
            g = ph["geom"]
            if not g:
                continue
            left = max(left, min(g["left"], w * 0.18))
            top = max(top, min(g["top"], h * 0.15))
            right = min(right, max(g["left"] + g["width"], w * 0.82))
            bottom = min(bottom, max(g["top"] + g["height"], h * 0.85))
    return {"left": round(left, 3), "top": round(top, 3),
            "right": round(right, 3), "bottom": round(bottom, 3)}


def _render_thumbs(path: Path, work_dir: Path) -> list[str]:
    thumb_dir = ensure_dir(work_dir / TPL_THUMBS_DIR)
    code, out, err = run_cmd(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(thumb_dir),
         str(path)], timeout=180)
    if code != 0:
        log.warning("template render failed: %s", err.strip()[:200])
        return []
    pdf = thumb_dir / (path.stem + ".pdf")
    if not pdf.exists():
        return []
    import fitz
    d = fitz.open(str(pdf))
    pngs = []
    for i, page in enumerate(d):
        pix = page.get_pixmap(matrix=fitz.Matrix(0.55, 0.55), alpha=False)
        p = thumb_dir / f"thumb_{i + 1:02d}.png"
        pix.save(str(p))
        pngs.append(str(p))
    return pngs


def inspect(path: Path | None, work_dir: Path, cjk: bool) -> dict:
    if path is None:
        spec = {
            "provided": False, **DEFAULT_THEME,
            "fonts": {
                "sans": resolve_font(None, cjk=False),
                "cjk_sans": resolve_font(None, cjk=True),
                "serif": resolve_font(None, cjk=False, serif=True),
                "cjk_serif": resolve_font(None, cjk=True, serif=True),
            },
            "layouts": [],
        }
        write_json(work_dir / TEMPLATE_JSON, spec)
        return spec

    from pptx import Presentation
    prs = Presentation(str(path))
    w = prs.slide_width / 914400
    h = prs.slide_height / 914400
    layouts = []
    for layout in prs.slide_layouts:
        phs = _placeholder_records(layout.placeholders)
        layouts.append({
            "name": layout.name, "placeholders": phs,
            "roles": _classify_layout(phs),
        })
    existing = []
    for i, slide in enumerate(prs.slides):
        existing.append({
            "slide_no": i + 1,
            "layout": slide.slide_layout.name,
            "shapes": len(slide.shapes),
            "placeholders": [
                {"idx": s.placeholder_format.idx,
                 "type": str(s.placeholder_format.type),
                 "has_text": bool(s.has_text_frame and s.text_frame.text.strip())}
                for s in slide.shapes if s.is_placeholder],
        })
    theme = _theme_info(prs.slide_masters[0]) if prs.slide_masters else {}
    cjk_font_hint = cjk or bool(theme.get("ea_font"))
    spec = {
        "provided": True,
        "source": str(path),
        "slide_w_in": round(w, 3), "slide_h_in": round(h, 3),
        "theme": theme,
        "fonts": {
            "sans": resolve_font(theme.get("latin_font"), cjk=False),
            "cjk_sans": resolve_font(theme.get("ea_font"), cjk=True),
            "serif": resolve_font(theme.get("latin_font"), cjk=False, serif=True),
            "cjk_serif": resolve_font(theme.get("ea_font"), cjk=True, serif=True),
        },
        "layouts": layouts,
        "safe_area": _safe_area(layouts, w, h),
        "existing_slides": existing,
        "thumbnails": _render_thumbs(path, work_dir),
    }
    write_json(work_dir / TEMPLATE_JSON, spec)
    return spec


def run(work_dir: Path | None = None) -> dict:
    work_dir = Path(work_dir or WORK_DIR)
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / "paper.json")
    cjk = (paper or {}).get("language") == "zh"
    deck = None
    if brief["roles"]["starting_decks"]:
        deck = Path(brief["roles"]["starting_decks"][0])
    elif brief["roles"]["templates"]:
        deck = Path(brief["roles"]["templates"][0])
    spec = inspect(deck, work_dir, cjk)
    changes = ChangeLog(work_dir)
    changes.add("template", "inspect",
                f"provided={spec['provided']} layouts={len(spec.get('layouts', []))}")
    log.info("template: provided=%s, %d layouts, %d existing slides",
             spec["provided"], len(spec.get("layouts", [])),
             len(spec.get("existing_slides", [])))
    return {"template": str(work_dir / TEMPLATE_JSON)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir")
    args = parser.parse_args()
    run(args.work_dir)


if __name__ == "__main__":
    main()
