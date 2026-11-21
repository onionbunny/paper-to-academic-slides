"""Stage 4 — build the editable academic deck with python-pptx.

The deck is built on the real frozen template/starting deck (masters, layouts,
theme colors and footers are never overwritten). Slides use real title
placeholders; wide content layouts are selected and their body placeholders are
replaced with native, contain-fitted pictures (never cropped), verbatim
captions, native tables and separated speaker notes. Deterministic text
fitting prevents overflow and the font policy prevents substitution on export.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Inches, Pt
from pptx.oxml.ns import qn

from common import (
    CASE_BRIEF,
    PAPER_JSON,
    SLIDE_PLAN,
    TEMPLATE_JSON,
    WORK_DIR,
    ChangeLog,
    get_logger,
    has_cjk,
    read_json,
    resolve_font,
    write_json,
)

log = get_logger("build")

DARK_GRAY = RGBColor(0x59, 0x59, 0x59)
TITLE_START, TITLE_FLOOR = 28, 18
BODY_START, BODY_FLOOR = 18, 11
TABLE_START, TABLE_FLOOR = 13, 9
CAPTION_PT = 10


# ---------------------------------------------------------------------------
# Font handling
# ---------------------------------------------------------------------------
def set_run_typefaces(run, latin: str | None = None, ea: str | None = None) -> None:
    rPr = run._r.get_or_add_rPr()
    for tag, name in ((qn("a:latin"), latin), (qn("a:ea"), ea), (qn("a:cs"), latin)):
        if not name:
            continue
        old = rPr.find(tag)
        if old is not None:
            rPr.remove(old)
        rPr.append(rPr.makeelement(tag, {"typeface": name}))


def font_policy(spec: dict, plan: dict) -> dict:
    cjk = plan.get("language") == "zh"
    theme = spec.get("theme", {})
    if not spec.get("provided"):
        latin = resolve_font(theme.get("minor_font") or "Calibri", cjk=False)
        ea = resolve_font(theme.get("minor_ea_font") or "Noto Sans CJK SC", cjk=True)
        return {"latin": latin, "ea": ea if cjk else None}
    ea_theme = theme.get("major_ea_font") or theme.get("minor_ea_font")
    if cjk and not ea_theme:
        return {"latin": None, "ea": resolve_font("Noto Sans CJK SC", cjk=True)}
    return {"latin": None, "ea": None}


# ---------------------------------------------------------------------------
# Deterministic text fitting
# ---------------------------------------------------------------------------
def _line_width_pt(text: str, pt: float) -> float:
    cjk = sum(1 for ch in text if has_cjk(ch))
    other = len(text) - cjk
    return cjk * pt + other * 0.52 * pt


def _needed_lines(text: str, box_w_pt: float, pt: float) -> int:
    total = 0
    for chunk in str(text).split("\n") or [""]:
        if not chunk:
            total += 1
            continue
        width = _line_width_pt(chunk, pt)
        total += max(1, int(-(-width // box_w_pt)))
    return total


def fit_size(paragraphs: list[tuple[str, int]], box_w_in: float, box_h_in: float,
             start: int, floor: int) -> tuple[int, bool]:
    box_w = max(1.0, box_w_in) * 72
    box_h = max(0.3, box_h_in) * 72
    for pt in range(start, floor - 1, -1):
        lines = 0
        for text, level in paragraphs:
            indent = (level or 0) * pt * 1.2
            lines += _needed_lines(text, max(40, box_w - indent), pt)
        if lines * pt * 1.22 <= box_h:
            return pt, False
    return floor, True


# ---------------------------------------------------------------------------
# Layout resolution
# ---------------------------------------------------------------------------
def _all_layouts(prs: Presentation, spec: dict):
    info_by_name = {l["name"]: l for l in spec.get("layouts", [])}
    out = []
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            out.append((layout, info_by_name.get(layout.name, {"placeholders": [], "roles": []})))
    return out


def _wide_body(info: dict) -> float:
    widths = [p["width"] for p in info.get("placeholders", [])
              if p.get("width") and ("BODY" in p["type"] or "OBJECT" in p["type"])]
    return max(widths, default=0.0)


def pick_layout(prs: Presentation, spec: dict, wanted: str):
    layouts = _all_layouts(prs, spec)

    def has_title(info):
        return any("TITLE" in p["type"] for p in info.get("placeholders", []))

    if wanted == "title":
        scored = [x for x in layouts if any("SUBTITLE" in p["type"] or "CENTER_TITLE" in p["type"]
                                            for p in x[1].get("placeholders", []))]
        return (scored or layouts)[0][0]
    if wanted == "section":
        pool = [x for x in layouts if has_title(x[1]) and
                not any("BODY" in p["type"] or "OBJECT" in p["type"]
                        for p in x[1].get("placeholders", []))]
        return (pool or [x for x in layouts if has_title(x[1])] or layouts)[0][0]
    if wanted == "blank":
        pool = [x for x in layouts if not x[1].get("placeholders")]
        return (pool or layouts)[0][0]
    # content / figure: title + widest body placeholder
    candidates = sorted((x for x in layouts if has_title(x[1])),
                        key=lambda x: _wide_body(x[1]), reverse=True)
    return (candidates or layouts)[0][0]


def find_placeholder(slide, type_contains: str):
    for ph in slide.placeholders:
        if type_contains.upper() in str(ph.placeholder_format.type).upper():
            return ph
    return None


def remove_shape(shape) -> None:
    el = shape._element
    el.getparent().remove(el)


# ---------------------------------------------------------------------------
# Content writers
# ---------------------------------------------------------------------------
_ACTIVE_POLICY: dict = {}


def apply_font(run, policy: dict | None = None) -> None:
    pol = policy or _ACTIVE_POLICY
    set_run_typefaces(run, latin=pol.get("latin"), ea=pol.get("ea"))


def fill_title(ph, text: str, box: tuple) -> list[str]:
    pt, overflow = fit_size([(text, 0)], box[2], box[3], TITLE_START, TITLE_FLOOR)
    tf = ph.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    run = tf.paragraphs[0].add_run()
    run.text = str(text)
    run.font.size = Pt(pt)
    run.font.bold = True
    apply_font(run)
    return [f"title at floor {pt}pt still overflow"] if overflow else []


def fill_bullets(ph, bullets: list[dict], box: tuple,
                 anchor: MSO_ANCHOR = MSO_ANCHOR.TOP) -> tuple[int, list[str]]:
    tf = ph.text_frame
    tf.word_wrap = True
    tf.auto_size = None
    tf.vertical_anchor = anchor
    paras = [(b.get("text", ""), b.get("level", 0)) for b in bullets]
    pt, overflow = fit_size(paras, box[2], box[3], BODY_START, BODY_FLOOR)
    for i, b in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = int(b.get("level", 0))
        p.space_after = Pt(6)
        run = p.add_run()
        run.text = b.get("text", "")
        run.font.size = Pt(pt)
        apply_font(run)
    return pt, [f"bullets at floor {pt}pt still overflow"] if overflow else []


def add_textbox(slide, left, top, width, height):
    return slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))


def contain_size(img_w: int, img_h: int, rect: tuple[float, float, float, float]) -> tuple:
    rl, rt, rw, rh = rect
    scale = min(rw / img_w, rh / img_h)
    w, h = img_w * scale, img_h * scale
    return rl + (rw - w) / 2, rt + (rh - h) / 2, w, h


def place_picture(slide, asset: str, rect: tuple) -> tuple:
    with Image.open(asset) as im:
        w_px, h_px = im.size
    l, t, w, h = contain_size(w_px, h_px, rect)
    return slide.shapes.add_picture(asset, Inches(l), Inches(t), Inches(w), Inches(h))


def _caption_height(caption: str, width_in: float, pt: int = CAPTION_PT) -> float:
    if not caption:
        return 0.0
    cjk = sum(1 for ch in caption if has_cjk(ch))
    text_w = cjk * pt + (len(caption) - cjk) * 0.52 * pt
    box_w = max(1.0, width_in) * 72
    lines = max(1, int(-(-text_w // box_w)), caption.count("\n") + 1)
    return min(1.15, lines * pt * 1.25 / 72 + 0.05)


def add_caption(slide, left: float, top: float, width: float,
                caption: str, height: float = 0.5, pt: int = CAPTION_PT) -> None:
    if not caption:
        return
    box = add_textbox(slide, left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    run = tf.paragraphs[0].add_run()
    run.text = caption[:380]
    run.font.size = Pt(pt)
    run.font.color.rgb = DARK_GRAY
    apply_font(run)


def add_source_label(slide, safe: dict, text: str) -> None:
    if not text:
        return
    box = add_textbox(slide, safe["left"], safe["bottom"] - 0.28, safe["width"], 0.26)
    tf = box.text_frame
    tf.word_wrap = True
    run = tf.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(9)
    run.font.color.rgb = DARK_GRAY
    apply_font(run)


# ---------------------------------------------------------------------------
# Native table
# ---------------------------------------------------------------------------
def place_native_table(slide, paper_table: dict, region: dict) -> list[str]:
    rows = paper_table["rows"]
    max_rows = max(1, int((region["height"] - 0.2) / 0.32))
    truncated = False
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    h = min(region["height"], 0.32 * len(rows) + 0.1)
    gframe = slide.shapes.add_table(len(rows), cols, Inches(region["left"]),
                                    Inches(region["top"]), Inches(region["width"]), Inches(h))
    table = gframe.table
    pt = TABLE_START if len(rows) <= 12 else TABLE_FLOOR + 1
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.margin_left = Inches(0.05); cell.margin_right = Inches(0.05)
            cell.margin_top = Inches(0.02); cell.margin_bottom = Inches(0.02)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            run = cell.text_frame.paragraphs[0].add_run()
            run.text = str(value)
            run.font.size = Pt(pt)
            if r == 0:
                run.font.bold = True
            apply_font(run)
    return [f"table {paper_table['id']} truncated to {max_rows} rows"] if truncated else []


# ---------------------------------------------------------------------------
# Per-slide building
# ---------------------------------------------------------------------------
def _ph_box(ph, safe: dict) -> tuple:
    l = ph.left / 914400 if ph.left is not None else safe["left"]
    t = ph.top / 914400 if ph.top is not None else safe["top"]
    w = ph.width / 914400 if ph.width is not None else safe["width"]
    h = ph.height / 914400 if ph.height is not None else safe["height"]
    return l, t, w, h


def _content_region(safe: dict, title_ph) -> dict:
    if title_ph is not None:
        tb = _ph_box(title_ph, safe)
        top = tb[1] + tb[3] + 0.15
    else:
        top = safe["top"] + 0.75
    return {"left": safe["left"], "top": top,
            "width": safe["width"],
            "height": safe["bottom"] - top - 0.32,
            "right": safe["right"], "bottom": safe["bottom"] - 0.32}


def build_slide(prs: Presentation, spec: dict, plan_slide: dict, paper: dict) -> list[str]:
    safe = spec["safe_area"]
    warnings: list[str] = []
    wanted = "title" if plan_slide["role"] == "title" else \
             "section" if plan_slide["role"] == "section" else \
             "blank" if plan_slide["role"] == "backup" else "content"
    layout = pick_layout(prs, spec, wanted)
    slide = prs.slides.add_slide(layout)
    title_ph = find_placeholder(slide, "TITLE")
    sub_ph = find_placeholder(slide, "SUBTITLE")

    # ---- title slide ----
    if plan_slide["role"] == "title":
        if title_ph:
            warnings += fill_title(title_ph, plan_slide["title"], _ph_box(title_ph, safe))
        sub_text = " ".join(b.get("text", "") for b in plan_slide.get("bullets", []))
        if sub_ph and sub_text:
            run = sub_ph.text_frame.paragraphs[0].add_run()
            run.text = sub_text
            run.font.size = Pt(16)
            apply_font(run)
        for ph in list(slide.placeholders):
            t = str(ph.placeholder_format.type)
            if "BODY" in t or "OBJECT" in t or "PICTURE" in t:
                remove_shape(ph)
        return warnings

    # ---- standard slides: keep the real title placeholder, drop body ones ----
    if title_ph:
        warnings += fill_title(title_ph, plan_slide["title"], _ph_box(title_ph, safe))
    else:
        # no title placeholder: manual box with the same adaptive sizing
        title_h = 1.05
        box = add_textbox(slide, safe["left"], safe["top"], safe["width"], title_h)
        warnings += fill_title(box, plan_slide["title"],
                               (safe["left"], safe["top"], safe["width"], title_h))
    for ph in list(slide.placeholders):
        t = str(ph.placeholder_format.type)
        if "BODY" in t or "OBJECT" in t or "PICTURE" in t:
            remove_shape(ph)

    region = _content_region(safe, title_ph)
    figures = plan_slide.get("figures", [])
    table_refs = plan_slide.get("tables", [])
    bullets = plan_slide.get("bullets", [])

    # ---- native table ----
    if table_refs and not figures:
        tbl_index = {t["id"]: t for t in paper.get("tables", [])}
        t = tbl_index.get(str(table_refs[0]["id"]))
        if t and t.get("rows"):
            warnings += place_native_table(slide, t, region)
        add_source_label(slide, safe, plan_slide.get("source_label", ""))
        return warnings

    # ---- figures ----
    if figures:
        if len(figures) == 1:
            cap_text = figures[0].get("caption_verbatim", "")
            cap_pt = CAPTION_PT
            cap_h = _caption_height(cap_text, region["width"], cap_pt)
            if cap_h > 0.9:
                cap_pt = 9
                cap_h = _caption_height(cap_text, region["width"], 9)
            upper_h = max(1.2, region["height"] - cap_h - 0.08)
            if bullets:
                fig_w = region["width"] * 0.58
                fig_rect = (region["left"], region["top"], fig_w, upper_h)
                b_rect = (region["left"] + fig_w + 0.3, region["top"],
                          region["width"] - fig_w - 0.3, upper_h)
                box = add_textbox(slide, *b_rect)
                _, bw = fill_bullets(box, bullets, b_rect)
                warnings += bw
            else:
                fig_rect = (region["left"], region["top"], region["width"], upper_h)
            place_picture(slide, figures[0]["asset_path"], fig_rect)
            add_caption(slide, region["left"], region["top"] + upper_h + 0.06,
                        region["width"], cap_text, height=cap_h, pt=cap_pt)
        else:
            col_w = (region["width"] - 0.25) / 2
            for i, ref in enumerate(figures[:4]):
                col, row = i % 2, i // 2
                cell = (region["left"] + col * (col_w + 0.25),
                        region["top"] + row * (region["height"] / 2),
                        col_w, region["height"] / 2 - 0.42)
                place_picture(slide, ref["asset_path"], cell)
                add_caption(slide, cell[0], cell[1] + cell[3] + 0.02, col_w,
                            ref.get("caption_verbatim", "")[:180], height=0.4)
        add_source_label(slide, safe, plan_slide.get("source_label", ""))
        return warnings

    # ---- pure text ----
    box = add_textbox(slide, region["left"], region["top"], region["width"], region["height"])
    _, bw = fill_bullets(box, bullets,
                         (region["left"], region["top"], region["width"], region["height"]))
    warnings += bw
    add_source_label(slide, safe, plan_slide.get("source_label", ""))
    return warnings


def set_notes(slide, text: str) -> None:
    if text:
        slide.notes_slide.notes_text_frame.text = str(text)


def build(work_dir: Path) -> dict:
    global _ACTIVE_POLICY
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / PAPER_JSON)
    spec = read_json(work_dir / TEMPLATE_JSON)
    plan = read_json(work_dir / SLIDE_PLAN)
    changes = ChangeLog(work_dir)

    source = brief.get("starting_pptx") or brief.get("template_pptx")
    if source:
        prs = Presentation(source)
        existing = list(prs.slides._sldIdLst)
        for sld_id in existing:
            rId = sld_id.get(qn("r:id"))
            prs.slides._sldIdLst.remove(sld_id)
            if rId:
                prs.part.drop_rel(rId)
        if existing:
            changes.add("build", "reset", f"removed {len(existing)} example slides from source deck")
    else:
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)

    _ACTIVE_POLICY = font_policy(spec, plan)
    all_warnings: list[str] = []
    for plan_slide in plan["slides"]:
        warns = build_slide(prs, spec, plan_slide, paper)
        set_notes(prs.slides[-1], plan_slide.get("notes", ""))
        all_warnings.extend(f"slide {plan_slide['slide_no']}: {w}" for w in warns)

    out_path = work_dir / "academic_deck.pptx"
    prs.save(str(out_path))
    report = {"deck": str(out_path), "slides": len(plan["slides"]),
              "font_policy": _ACTIVE_POLICY, "warnings": all_warnings}
    write_json(work_dir / "build_report.json", report)
    changes.add("build", "save", f"{out_path.name} with {len(plan['slides'])} slides")
    log.info("saved %s (%d slides), warnings=%d", out_path, len(plan["slides"]), len(all_warnings))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    build(Path(args.work_dir))


if __name__ == "__main__":
    main()
