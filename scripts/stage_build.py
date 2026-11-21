"""Build the editable PPTX from a validated slide plan.

Three source modes are handled honestly:
  * starting deck / template: the real file is opened, its master, theme and
    size preserved, slides built on its layouts;
  * no deck: a fresh 16:9 presentation built on the scholar-default theme.

Figures are real image files, tables are native editable PowerPoint tables,
and speaker notes are embedded on every slide. Missing assets become
explicit placeholders, never fabricated images.
"""
from __future__ import annotations

from pathlib import Path

from common import (
    BUILD_REPORT, CASE_BRIEF, OUTPUT_DIR, PAPER_JSON, SLIDE_PLAN, TEMPLATE_JSON,
    ChangeLog, get_logger, read_json, write_json,
)
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt, Emu

LOG = get_logger("build")

DEFAULT_COLORS = {
    "ink": "1F2A37", "primary": "173F5F", "accent": "2E86AB",
    "accent2": "E07A5F", "soft": "F4F6F8", "rule": "D9DEE4", "muted": "6B7785",
}
MAX_TABLE_COLS = 9
MAX_TABLE_ROWS = 14


def _rgb(hex_value: str) -> RGBColor:
    return RGBColor.from_string((hex_value or "1F2A37").lstrip("#"))


def _set_run_font(run, fonts: dict, size: int, color: RGBColor, bold: bool, cjk: bool):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    family = fonts["cjk_sans"] if cjk else fonts["sans"]
    run.font.name = family
    # bind the east-asian font slot as well so CJK glyphs render correctly
    try:
        from pptx.oxml.ns import qn
        rpr = run._r.get_or_add_rPr()
        for tag in ("a:latin", "a:ea", "a:cs"):
            el = rpr.find(qn(tag))
            if el is None:
                el = rpr.makeelement(qn(tag), {})
                rpr.append(el)
            el.set("typeface", family)
    except Exception:
        pass


def _add_text(slide, left, top, width, height, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    return tf


def _fill_paragraph(para, text, fonts, size, color, bold=False, cjk=False, level=0):
    run = para.add_run()
    run.text = text
    _set_run_font(run, fonts, size, color, bold, cjk)
    para.level = level
    para.space_after = Pt(6)


def _add_rule(slide, left, top, width, color):
    line = slide.shapes.add_connector(1, Inches(left), Inches(top), Inches(left + width), Inches(top))
    line.line.color.rgb = color
    line.line.width = Pt(1.5)


def _pick_layout(prs, tpl, role):
    layouts = tpl.get("layouts") or []
    if not layouts:
        return prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[0]
    want = {"title": ["title_only", "title_content", "title_body"],
            "divider": ["title_only", "blank"],
            }.get(role, ["title_content", "title_body", "title_only", "custom", "blank"])
    for tag in want:
        for i, layout in enumerate(layouts):
            if tag in layout.get("roles", []):
                return prs.slide_layouts[i]
    return prs.slide_layouts[0]


def _remove_empty_placeholders(slide):
    for shape in list(slide.placeholders):
        if not (shape.has_text_frame and shape.text_frame.text.strip()):
            shape._element.getparent().remove(shape._element)


def _title_placeholder(slide):
    for shape in slide.placeholders:
        if "title" in str(shape.placeholder_format.type).lower():
            return shape
    return None


def _add_figure(slide, zone, paper, fig_id, colors, fonts):
    fig = next((f for f in paper["figures"] if f["id"] == fig_id), None)
    if not fig:
        return f"{fig_id} not found in extraction"
    asset = fig.get("asset") or {}
    path = asset.get("file")
    l, t, w, h = zone
    if not path or not Path(path).exists():
        rect = slide.shapes.add_shape(1, Inches(l), Inches(t), Inches(w), Inches(h))
        rect.fill.solid(); rect.fill.fore_color.rgb = _rgb(colors["soft"])
        rect.line.color.rgb = _rgb(colors["rule"])
        tf = rect.text_frame; tf.text = f"[ {fig_id} asset unavailable ]"
        return f"{fig_id} asset missing; placeholder rendered"
    from PIL import Image
    with Image.open(path) as im:
        iw, ih = im.size
    scale = min(w / (iw / 96.0), h / (ih / 96.0))
    pw, ph = (iw / 96.0) * scale, (ih / 96.0) * scale
    slide.shapes.add_picture(path, Inches(l + (w - pw) / 2), Inches(t + (h - ph) / 2),
                             width=Inches(pw), height=Inches(ph))
    return None


def _normalize_rows(rows):
    clean = []
    for row in rows:
        cells = [(c or "").strip() for c in row]
        if len(cells) > MAX_TABLE_COLS:
            cells = cells[:MAX_TABLE_COLS - 1] + [" ".join(cells[MAX_TABLE_COLS - 1:])]
        clean.append(cells)
    width = max((len(r) for r in clean), default=1)
    clean = [r + [""] * (width - len(r)) for r in clean][:MAX_TABLE_ROWS]
    return _tidy_columns(clean)


def _tidy_columns(rows):
    """Merge a column that is empty in every data row into its left neighbor,
    joining header words (fixes split headers such as 'Maximum Path'/'Length')."""
    if len(rows) < 2:
        return rows, max((len(r) for r in rows), default=1)
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    data = rows[1:]
    c = 0
    while c < ncols:
        col_empty = all(not (r[c] or "").strip() for r in data)
        if col_empty and c > 0:
            for r in rows:
                if r[c].strip():
                    r[c - 1] = (r[c - 1] + " " + r[c]).strip()
            for r in rows:
                r.pop(c)
            ncols -= 1
            continue
        c += 1
    return rows, ncols


def _add_table(slide, zone, paper, tab_id, colors, fonts, cjk):
    tab = next((t for t in paper["tables"] if t["id"] == tab_id), None)
    if not tab:
        return f"{tab_id} not found"
    rows, ncols = _normalize_rows(tab.get("rows", []))
    if not rows:
        return f"{tab_id} has no extracted rows"
    l, t, w, h = zone
    nrows = len(rows)
    gfx = slide.shapes.add_table(nrows, ncols, Inches(l), Inches(t), Inches(w), Inches(h))
    table = gfx.table
    font_size = 11 if ncols <= 5 else (9.5 if ncols <= 7 else 8.5)
    # compact, explicit row heights so rows do not stretch across the zone
    row_h = Pt(26 if nrows <= 8 else 20)
    for r in range(nrows):
        table.rows[r].height = row_h
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.margin_left = Inches(0.05); cell.margin_right = Inches(0.05)
            cell.margin_top = Inches(0.02); cell.margin_bottom = Inches(0.02)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(colors["soft"]) if r == 0 else RGBColor(0xFF, 0xFF, 0xFF)
            tf = cell.text_frame; tf.word_wrap = True
            para = tf.paragraphs[0]
            _fill_paragraph(para, value[:120], fonts, font_size,
                            _rgb(colors["ink"]), bold=(r == 0), cjk=cjk)
    return None


def _title_lines(title: str, width_in: float) -> int:
    """Estimate wrapped line count for a 26pt bold title in the given width."""
    import math
    # 26pt bold average glyph advance ~0.52em; width available in points
    chars_per_line = max(12, int((width_in * 72) / (26 * 0.55)))
    return min(3, max(1, math.ceil(len(title) / chars_per_line)))


def build(brief, paper, tpl, plan, output_dir: Path) -> dict:
    colors = tpl.get("colors") or DEFAULT_COLORS
    colors = {**DEFAULT_COLORS, **colors}
    fonts = tpl.get("fonts") or {"sans": "Liberation Sans", "cjk_sans": "Noto Sans CJK SC"}
    cjk = plan.get("language") == "zh"
    safe = tpl.get("safe_area", {"left": 0.62, "top": 0.52, "right": 12.72, "bottom": 6.92})

    source_deck = None
    for key in ("starting_decks", "templates"):
        if brief.get("roles", {}).get(key):
            source_deck = brief["roles"][key][0]
            break

    if source_deck and Path(source_deck).exists():
        prs = Presentation(source_deck)
        # drop existing slides but keep the master/layouts/theme
        sldIdLst = prs.slides._sldIdLst
        for sldId in list(sldIdLst):
            prs.part.drop_rel(sldId.rId)
            sldIdLst.remove(sldId)
        theme_mode = "provided"
    else:
        prs = Presentation()
        prs.slide_width = Inches(tpl.get("slide_w_in", 13.333))
        prs.slide_height = Inches(tpl.get("slide_h_in", 7.5))
        theme_mode = "scholar-default"

    findings = []
    for spec in plan["slides"]:
        role = spec["role"]
        layout = _pick_layout(prs, tpl, role)
        slide = prs.slides.add_slide(layout)
        title_ph = _title_placeholder(slide)
        # Guard against divider-style layouts whose title sits in the lower
        # half of the slide: drop it and use a correctly positioned manual title.
        if title_ph is not None and title_ph.top is not None:
            top_in = title_ph.top / 914400
            if top_in > tpl.get("slide_h_in", 7.5) * 0.5:
                title_ph._element.getparent().remove(title_ph._element)
                title_ph = None
        if title_ph is not None:
            title_ph.text = spec["title"]
            for para in title_ph.text_frame.paragraphs:
                for run in para.runs:
                    _set_run_font(run, fonts, 26, _rgb(colors["primary"]), True, cjk)
        _remove_empty_placeholders(slide)

        s_l, s_t, s_r, s_b = safe["left"], safe["top"], safe["right"], safe["bottom"]
        s_w = s_r - s_l
        title_lines = _title_lines(spec["title"], s_w)
        title_h = 0.42 * title_lines
        if title_ph is None:
            # manual claim title
            tf = _add_text(slide, s_l, s_t, s_w, title_h + 0.15)
            para = tf.paragraphs[0]
            _fill_paragraph(para, spec["title"], fonts, 26, _rgb(colors["primary"]), bold=True, cjk=cjk)
            _add_rule(slide, s_l, s_t + title_h + 0.12, s_w, _rgb(colors["primary"]))
            content_top = s_t + title_h + 0.28
        else:
            content_top = s_t + title_h + 0.45

        has_evidence = bool(spec["figures"] or spec["tables"])
        if has_evidence:
            text_w = 4.6
            zone_left = s_l + text_w + 0.35
            zone = (zone_left, content_top, s_r - zone_left, s_b - content_top - 0.25)
        else:
            text_w = s_w
        tf = _add_text(slide, s_l, content_top, text_w, s_b - content_top - 0.2)
        first = True
        for bullet in spec["bullets"]:
            para = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            _fill_paragraph(para, ("• " if bullet.get("level", 0) == 0 else "– ") + bullet["text"],
                            fonts, 15 if bullet.get("level", 0) == 0 else 13,
                            _rgb(colors["ink"]), cjk=cjk, level=bullet.get("level", 0))

        for fig_id in spec["figures"]:
            issue = _add_figure(slide, zone, paper, fig_id, colors, fonts)
            if issue:
                findings.append(issue)
        for tab_id in spec["tables"]:
            issue = _add_table(slide, zone, paper, tab_id, colors, fonts, cjk)
            if issue:
                findings.append(issue)

        if spec.get("source_label"):
            lab = _add_text(slide, s_l, s_b - 0.02, s_w, 0.3)
            _fill_paragraph(lab.paragraphs[0], spec["source_label"], fonts, 10,
                            _rgb(colors["muted"]), cjk=cjk)
        if spec.get("backup"):
            tag = _add_text(slide, s_r - 1.2, s_t - 0.05, 1.2, 0.3)
            p = tag.paragraphs[0]; p.alignment = PP_ALIGN.RIGHT
            _fill_paragraph(p, "BACKUP", fonts, 10, _rgb(colors["muted"]), bold=True, cjk=cjk)

        notes = spec.get("notes", "")
        slide.notes_slide.notes_text_frame.text = notes

    output_dir.mkdir(parents=True, exist_ok=True)
    deck_path = output_dir / "deck.pptx"
    prs.save(str(deck_path))
    report = {
        "deck": str(deck_path), "slides": len(plan["slides"]),
        "theme_mode": theme_mode, "source_deck": source_deck,
        "findings": findings,
    }
    write_json(output_dir / BUILD_REPORT, report)
    return report


def run(work_dir=None, output_dir=None):
    work_dir = Path(work_dir)
    output_dir = Path(output_dir or OUTPUT_DIR)
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / PAPER_JSON)
    tpl = read_json(work_dir / TEMPLATE_JSON)
    plan = read_json(work_dir / SLIDE_PLAN)
    report = build(brief, paper, tpl, plan, output_dir)
    ChangeLog(work_dir).add("build", "deck built", f"{report['slides']} slides, {report['theme_mode']}")
    LOG.info("built %s (%d slides)", report["deck"], report["slides"])
    return report


if __name__ == "__main__":
    import sys
    from common import WORK_DIR
    args = sys.argv[1:]
    run(Path(args[0]) if args else WORK_DIR,
        Path(args[1]) if len(args) > 1 else OUTPUT_DIR)
