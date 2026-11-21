"""Stage 2 — scientific paper extraction.

Parses the real PDF (no pre-extracted answer layer):
  * per-page text, TOC and metadata; encrypted / scanned detection
  * real abstract (heading-based, not a dump of page one)
  * figure/table captions (English and Chinese), verbatim, with stable ids
  * in-text figure/table mentions with sentence context
  * figure region crops at 200 DPI (vector + raster), white-margin trimmed,
    tagged schematic/data/photo and own-result/cited-work
  * embedded raster images as a fallback asset source
  * tables via pdfplumber (real rows; numbers are never re-typed)
  * per-page number/unit index for later evidence verification

Outputs paper.json, assets/ and a bounded paper_review.md planning digest.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz  # PyMuPDF

from common import (
    CASE_BRIEF, PAPER_JSON, PAPER_REVIEW, ASSETS_DIR, WORK_DIR,
    ChangeLog, cjk_ratio, ensure_dir, get_logger, has_cjk,
    merge_rects, normalize_text, read_json, rects_overlap, resolve_font,
    run_cmd, truncate_words, which, write_json,
)

log = get_logger("extract")

# ---------------------------------------------------------------------------
# Label patterns (Figure / Fig. / Table / 图 / 表, incl. supplement S1)
# ---------------------------------------------------------------------------
_NUM = r"([Ss]?\d+(?:\.\d+)?[A-Za-z]?)"
FIG_LABEL = rf"(?:fig(?:ure)?\.?\s*{_NUM}|图\s*{_NUM})"
TBL_LABEL = rf"(?:table\s*{_NUM}|表\s*{_NUM})"
CAPTION_FIG_RE = re.compile(r"^\s*" + FIG_LABEL, re.I)
CAPTION_TBL_RE = re.compile(r"^\s*" + TBL_LABEL, re.I)
MENTION_FIG_RE = re.compile(FIG_LABEL, re.I)
MENTION_TBL_RE = re.compile(TBL_LABEL, re.I)
_NEXT_CHAR_RE = re.compile(
    rf"^\s*(?:fig(?:ure)?|table)\.?\s*{_NUM}\s*([:.。：])?\s*(.)", re.I)
_LABEL_EOL_RE = re.compile(rf"^\s*(?:fig(?:ure)?|table)\.?\s*{_NUM}\s*[:.。：]?\s*$", re.I)
_CJK_CAPTION_RE = re.compile(rf"^\s*(?:图|表)\s*{_NUM}")
_CJK_MENTION_RE = re.compile(r"(?:如|见|参见|参考)\s*(?:图|表)")

SCHEMATIC_RE = re.compile(
    r"schematic|diagram|workflow|conceptual|illustration|framework|model architecture|示意|流程|概念|框架",
    re.I)
PHOTO_RE = re.compile(r"photo|micrograph|histology|image of|photograph|显微|照片", re.I)
CITED_RE = re.compile(
    r"et\s*al\.|reproduced|adapted|reprinted|with permission|modified from|redrawn|来源|引用|转载",
    re.I)

MIN_GRAPHIC_PT = 12.0          # ignore marks smaller than ~12 pt
CROP_DPI = 200
MIN_CROP_PX = 120
SCANNED_CHAR_THRESHOLD = 120

NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:%|[a-zA-ZμµΩ/²³·\.]+)?")


# ---------------------------------------------------------------------------
# Text / block helpers
# ---------------------------------------------------------------------------
def _block_lines(block: dict) -> list[dict]:
    lines = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", []))
        if text.strip():
            lines.append({"text": text, "bbox": tuple(line["bbox"])})
    return lines


def _page_blocks(page: fitz.Page) -> list[dict]:
    blocks = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        lines = _block_lines(block)
        if lines:
            x0 = min(l["bbox"][0] for l in lines)
            y0 = min(l["bbox"][1] for l in lines)
            x1 = max(l["bbox"][2] for l in lines)
            y1 = max(l["bbox"][3] for l in lines)
            blocks.append({"lines": lines, "bbox": (x0, y0, x1, y1)})
    return blocks


def _label_id(match: re.Match) -> str:
    groups = [g for g in match.groups() if g]
    return groups[0] if groups else ""


def _is_caption_start(first_line: str) -> bool:
    if _LABEL_EOL_RE.match(first_line):
        return True
    m = _NEXT_CHAR_RE.match(first_line)
    if m:
        # group(1) is the number itself; separator and char follow
        separator, ch = m.group(2), m.group(3)
        if separator or ch in ",，":
            return True
        if ch.isupper():
            return True
        if ch.islower():
            return False
    if _CJK_CAPTION_RE.match(first_line) and not _CJK_MENTION_RE.search(first_line[:6]):
        return True
    return False


def _kind_prefix(text: str) -> str:
    return "fig" if text.lstrip()[:1].lower() == "f" or text.lstrip()[:1] == "图" else "tab"


def _find_captions(page: fitz.Page, page_no: int) -> list[dict]:
    blocks = _page_blocks(page)
    captions = []
    for idx, block in enumerate(blocks):
        first = block["lines"][0]["text"]
        fig_m = CAPTION_FIG_RE.match(first)
        tbl_m = CAPTION_TBL_RE.match(first)
        match = fig_m or tbl_m
        if not match or not _is_caption_start(first):
            continue
        kind = "figure" if fig_m else "table"
        number = _label_id(match)
        cap_id = ("fig" if kind == "figure" else "tab") + number.lower()
        text_parts = [l["text"] for l in block["lines"]]
        bbox = block["bbox"]
        # absorb a continuation block (multi-column layouts split captions)
        nxt = blocks[idx + 1] if idx + 1 < len(blocks) else None
        if nxt:
            gap = nxt["bbox"][1] - bbox[3]
            line_h = bbox[3] - bbox[1]
            nxt_first = nxt["lines"][0]["text"]
            if (gap < max(14.0, line_h) and not CAPTION_FIG_RE.match(nxt_first)
                    and not CAPTION_TBL_RE.match(nxt_first)
                    and not _looks_like_heading(nxt_first)):
                text_parts.extend(l["text"] for l in nxt["lines"])
                bbox = (min(bbox[0], nxt["bbox"][0]), bbox[1],
                        max(bbox[2], nxt["bbox"][2]), nxt["bbox"][3])
        captions.append({
            "id": cap_id, "kind": kind, "number": number,
            "page": page_no, "bbox": bbox,
            "label_bbox": block["lines"][0]["bbox"],
            "label": ("Fig" if kind == "figure" else "Table") + " " + number,
            "text": normalize_text(" ".join(text_parts)),
        })
    return captions


_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*\s+[A-Z]|[A-Z][A-Z ]{4,}$|[\u4e00-\u9fff]{2,12}$)")


def _looks_like_heading(text: str) -> bool:
    return bool(_HEADING_RE.match(text.strip()))


# ---------------------------------------------------------------------------
# Graphic regions and cropping
# ---------------------------------------------------------------------------
def _graphic_rects(page: fitz.Page) -> list[tuple]:
    rects: list[tuple] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") == 1:  # raster image block
            r = tuple(block["bbox"])
            if r[2] - r[0] >= MIN_GRAPHIC_PT and r[3] - r[1] >= MIN_GRAPHIC_PT:
                rects.append(r)
    for drawing in page.get_drawings():
        r = drawing.get("rect")
        if r is None:
            continue
        tup = (r.x0, r.y0, r.x1, r.y1)
        if tup[2] - tup[0] >= MIN_GRAPHIC_PT and tup[3] - tup[1] >= MIN_GRAPHIC_PT:
            rects.append(tup)
    return merge_rects(rects, gap=4.0)


MIN_USABLE_W = 60.0
MIN_USABLE_H = 40.0


def _usable(r: tuple) -> bool:
    return (r[2] - r[0]) >= MIN_USABLE_W and (r[3] - r[1]) >= MIN_USABLE_H


def _band_union(band: tuple, clusters: list[tuple]) -> tuple | None:
    inside = [r for r in clusters if rects_overlap(r, band)]
    band_w, band_h = band[2] - band[0], band[3] - band[1]
    if inside:
        x0 = max(band[0], min(r[0] for r in inside))
        y0 = max(band[1], min(r[1] for r in inside))
        x1 = min(band[2], max(r[2] for r in inside))
        y1 = min(band[3], max(r[3] for r in inside))
        u_w, u_h = x1 - x0, y1 - y0
        if u_w >= MIN_USABLE_W and u_h >= MIN_USABLE_H:
            # tight union when it really covers the band; otherwise the figure
            # is built of sub-threshold glyphs and the whole caption-bounded
            # band is the faithful region
            if (u_w / band_w >= 0.55 and u_h / band_h >= 0.6) or len(inside) < 4:
                return (x0, y0, x1, y1)
            span_h = max(r[3] for r in inside) - min(r[1] for r in inside)
            if span_h / max(1.0, band_h) >= 0.25:
                return band
    # a band with zero graphics is plain text and must not be cropped
    return None


def _gap_to(r: tuple, cap: tuple) -> tuple[float, str]:
    """Vertical gap between a cluster and a caption; 'x' is x-affinity."""
    if r[3] <= cap[1]:
        gap = cap[1] - r[3]
    elif r[1] >= cap[3]:
        gap = r[1] - cap[3]
    else:
        gap = 0.0
    x_overlap = max(0.0, min(r[2], cap[2]) - max(r[0], cap[0]))
    affinity = x_overlap / max(1.0, min(r[2] - r[0], cap[2] - cap[0]))
    return gap - 25 * affinity, ("above" if r[3] <= cap[1] else "below")


def _side_area(rects: list[tuple], cap: tuple) -> dict:
    totals = {"above": 0.0, "below": 0.0}
    for r in rects:
        side = "above" if r[3] <= cap[1] else "below"
        totals[side] += (r[2] - r[0]) * (r[3] - r[1])
    return totals


def _pair_all_regions(captions: list[dict], clusters: list[tuple],
                      page_rect: tuple) -> dict[str, tuple]:
    """Assign every graphic cluster to a caption, then derive each caption's
    region. Assignment is nearest-caption with a dominance correction: when a
    caption already has a large figure on one side, tiny clusters on the other
    side are moved to their next-best caption (this disambiguates legend
    markers sitting between two captions)."""
    candidates_by_rect: dict[tuple, list[tuple]] = {}
    for r in clusters:
        scored = []
        for c in captions:
            gap, side = _gap_to(r, c["bbox"])
            scored.append((gap, c["id"], side))
        scored.sort(key=lambda s: s[0])
        candidates_by_rect[r] = scored

    assigned: dict[str, list[tuple]] = {c["id"]: [] for c in captions}
    for r, scored in candidates_by_rect.items():
        if scored and scored[0][0] < 120:
            assigned[scored[0][1]].append(r)

    # dominance correction, reassigning rejected clusters (bounded rounds)
    for _ in range(3):
        moves = []
        for c in captions:
            rects = assigned[c["id"]]
            totals = _side_area(rects, c["bbox"])
            if totals["above"] > 0 and totals["below"] > 0:
                weak_side = "below" if totals["below"] < totals["above"] else "above"
                weak_area = totals[weak_side]
                strong_area = max(totals.values())
                if weak_area < 0.15 * strong_area:
                    for r in list(rects):
                        _, _, side = candidates_by_rect[r][0]
                        if side == weak_side:
                            moves.append((c["id"], r))
        if not moves:
            break
        for old_id, r in moves:
            assigned[old_id].remove(r)
            for _, new_id, _ in candidates_by_rect[r][1:]:
                if r not in assigned[new_id]:
                    assigned[new_id].append(r)
                    break

    regions: dict[str, tuple] = {}
    for c in captions:
        cap = c["bbox"]
        mine = assigned[c["id"]]
        others = [x["bbox"] for x in captions if x["id"] != c["id"]]
        top_bound = page_rect[1]
        bottom_bound = page_rect[3]
        for b in others:
            if b[3] <= cap[1]:
                top_bound = max(top_bound, b[3])
            if b[1] >= cap[3]:
                bottom_bound = min(bottom_bound, b[1])
        left = max(0.0, cap[0] - 14)
        right = min(page_rect[2], cap[2] + 14)
        band = (left, top_bound + 2, right, bottom_bound - 2)
        if mine:
            x0 = max(band[0], min(r[0] for r in mine))
            y0 = max(band[1], min(r[1] for r in mine))
            x1 = min(band[2], max(r[2] for r in mine))
            y1 = min(band[3], max(r[3] for r in mine))
            u_w, u_h = x1 - x0, y1 - y0
            band_w, band_h = band[2] - band[0], band[3] - band[1]
            if u_w >= MIN_USABLE_W and u_h >= MIN_USABLE_H:
                if u_w / band_w >= 0.55 and u_h / band_h >= 0.6:
                    regions[c["id"]] = (x0, y0, x1, y1)
                    continue
                if len(mine) >= 4:  # sub-threshold glyph figure: full band
                    regions[c["id"]] = band
                    continue
                regions[c["id"]] = (x0, y0, x1, y1)
    return regions


def _pair_region(caption: dict, clusters: list[tuple], page_rect: tuple,
                 all_captions: list[dict] | None = None) -> tuple | None:
    """Single-caption wrapper kept for compatibility / local debugging."""
    caps = all_captions or [caption]
    return _pair_all_regions(caps, clusters, page_rect).get(caption["id"])


def _trim_white(path: Path, pad: int = 8) -> dict:
    from PIL import Image, ImageChops
    img = Image.open(path).convert("RGB")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    diff = ImageChops.difference(img, bg)
    bbox = diff.getbbox()
    if bbox:
        left = max(0, bbox[0] - pad); top = max(0, bbox[1] - pad)
        right = min(img.width, bbox[2] + pad); bottom = min(img.height, bbox[3] + pad)
        img.crop((left, top, right, bottom)).save(path)
        return {"width": right - left, "height": bottom - top}
    return {"width": img.width, "height": img.height}


def _crop_region(page: fitz.Page, region: tuple, out_path: Path, dpi: int = CROP_DPI) -> dict:
    zoom = dpi / 72.0
    clip = fitz.Rect(*region)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    ensure_dir(out_path.parent)
    pix.save(str(out_path))
    info = _trim_white(out_path)
    info["file"] = str(out_path)
    return info


def _embedded_images(doc: fitz.Page, page_no: int, assets: Path) -> list[dict]:
    out = []
    page = doc[page_no - 1]
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        try:
            base = doc.extract_image(xref)
        except Exception:
            continue
        ext = base.get("ext", "png")
        w, h = base.get("width", 0), base.get("height", 0)
        if w < MIN_CROP_PX or h < MIN_CROP_PX:
            continue
        path = assets / f"p{page_no:02d}_xref{xref}.{ext}"
        path.write_bytes(base["image"])
        out.append({"file": str(path), "width": w, "height": h, "xref": xref})
    return out


# ---------------------------------------------------------------------------
# Mentions, numbers, tables
# ---------------------------------------------------------------------------
def _mentions(text: str, page_no: int) -> list[dict]:
    findings = []
    for regex, kind in ((MENTION_FIG_RE, "figure"), (MENTION_TBL_RE, "table")):
        for m in regex.finditer(text):
            start = max(0, m.start() - 90); end = min(len(text), m.end() + 90)
            findings.append({
                "kind": kind, "id": ("fig" if kind == "figure" else "tab") +
                _label_id(m).lower(), "page": page_no,
                "context": normalize_text(text[start:end]),
            })
    return findings


def _number_index(text: str, page_no: int) -> list[dict]:
    items = []
    for m in NUMBER_RE.finditer(text):
        token = m.group().strip()
        if len(token) <= 1:
            continue
        start = max(0, m.start() - 60); end = min(len(text), m.end() + 60)
        items.append({"token": token, "context": normalize_text(text[start:end])})
    return items[:120]


def _horizontal_rules(page: fitz.Page) -> list[tuple]:
    out = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width > 110 and r.height < 2.5:
            out.append((r.x0, r.y0, r.x1, r.y1))
    return out


def _ruled_table_region(page: fitz.Page, cap: dict) -> tuple | None:
    """Precise region from real horizontal rulings. The seed is the rule
    closest to the caption; only rules sharing its x-range are grouped, which
    excludes running-header hairlines sitting far above the table."""
    rules = _horizontal_rules(page)
    # use the caption label line (the block bbox may already include table rows)
    label_cy = (cap["label_bbox"][1] + cap["label_bbox"][3]) / 2
    if not rules:
        return None
    seed = min(rules, key=lambda r: abs(((r[1] + r[3]) / 2) - label_cy))
    sx0, sx1 = seed[0], seed[2]
    group = [r for r in rules
             if abs(r[0] - sx0) <= 10 and abs(r[2] - sx1) <= 10
             and label_cy - 100 <= (r[1] + r[3]) / 2 <= label_cy + 450]
    # dedupe near-identical rules (same line exported twice)
    ys = sorted({round((r[1] + r[3]) / 2, 1) for r in group})
    x0 = min(r[0] for r in group) - 3; x1 = max(r[2] for r in group) + 3
    return (x0, min(ys) - 5, x1, max(ys) + 5)


def _band_region(page: fitz.Page, cap: dict) -> tuple:
    """Largest text-free band adjacent to a caption (fallback crop / table band)."""
    cap_b = cap["bbox"]
    upper = page.rect.y0 + 26
    for b in sorted(_page_blocks(page), key=lambda b: b["bbox"][3], reverse=True):
        if b["bbox"][3] <= cap_b[1] and cap_b[1] - b["bbox"][3] > 3:
            upper = b["bbox"][3]
            break
    left = max(60, cap_b[0] - 12); right = min(page.rect.width - 30, cap_b[2] + 12)
    above = (left, upper, right, cap_b[1] - 3)
    if above[3] - above[1] > 55:
        return above
    lower = page.rect.y1 - 26
    for b in sorted(_page_blocks(page), key=lambda b: b["bbox"][1]):
        if b["bbox"][1] >= cap_b[3] and b["bbox"][1] - cap_b[3] > 3:
            lower = b["bbox"][1]
            break
    return (left, cap_b[3] + 3, right, lower)


def _cluster_1d(values: list[float], tol: float) -> list[float]:
    anchors: list[float] = []
    for v in sorted(values):
        if not anchors or abs(v - anchors[-1]) > tol:
            anchors.append(v)
    return anchors


def _words_in(words: list, region: tuple) -> list:
    return [w for w in words
            if w[1] >= region[1] - 2 and w[3] <= region[3] + 2
            and w[0] >= region[0] - 24 and w[2] <= region[2] + 24]


def _header_anchors(words: list, row_anchors: list[float]) -> list[float]:
    """Column anchors come from the table's header rows: words in the first
    two rows are clustered with a wide gap (50pt) so multi-word headers
    ('Layer Type') become one anchor while real column gaps split."""
    if len(row_anchors) < 2:
        return []
    cutoff = row_anchors[1] + 3
    h_x = sorted({w[0] for w in words if (w[1] + w[3]) / 2 <= cutoff})
    anchors = []
    for x in h_x:
        if not anchors or x - anchors[-1] > 50:
            anchors.append(x)
    return anchors


def _column_anchors(words: list) -> list[float]:
    """Fallback for headerless tables: histogram local maxima."""
    xs = [w[0] for w in words]
    lo, hi = min(xs), max(xs)
    step = 2.0
    nb = max(8, int((hi - lo) / step) + 1)
    counts = [0] * nb
    for x in xs:
        counts[min(nb - 1, int((x - lo) / step))] += 1
    smooth = [sum(counts[max(0, i - 1):i + 2]) for i in range(nb)]
    anchors = []
    for i, val in enumerate(smooth):
        if val >= 3 and (i == 0 or val >= smooth[i - 1]) and (
                i == nb - 1 or val >= smooth[i + 1]):
            peak = lo + i * step
            if not anchors or peak - anchors[-1] >= 20:
                anchors.append(peak)
    return anchors[:12]


def _grid_from_words(words: list) -> list[list[str]]:
    if len(words) < 4:
        return []
    yvals = [(w[1] + w[3]) / 2 for w in words]
    row_anchors = _cluster_1d(yvals, tol=3.0)
    col_anchors = _header_anchors(words, row_anchors) or _column_anchors(words)
    if len(col_anchors) < 2:
        return []
    grid: dict[tuple[int, int], list] = {}
    for w in words:
        r = min(range(len(row_anchors)),
                key=lambda i: abs(row_anchors[i] - (w[1] + w[3]) / 2))
        before = [i for i, a in enumerate(col_anchors) if a <= w[0] + 3]
        c = before[-1] if before else min(
            range(len(col_anchors)), key=lambda i: abs(col_anchors[i] - w[0]))
        grid.setdefault((r, c), []).append(w)
    rows = []
    for ri in range(len(row_anchors)):
        row = [" ".join(w[4] for w in sorted(grid.get((ri, ci), []), key=lambda w: w[0]))
               for ci in range(len(col_anchors))]
        if any(x.strip() for x in row):
            rows.append(row)
    if rows:  # drop columns that ended up entirely empty
        keep = [ci for ci in range(len(col_anchors)) if any(r[ci].strip() for r in rows)]
        rows = [[r[ci] for ci in keep] for r in rows]
    return rows


def _word_grid_table(page: fitz.Page, cap: dict, region: tuple | None = None) -> list[list[str]]:
    """Reconstruct a table from real word geometry; ruled region when available."""
    region = region or _band_region(page, cap)
    return _grid_from_words(_words_in(page.get_text("words"), region))


def _bbox_iou(a: tuple, b: tuple) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter)


def _extract_tables(doc: fitz.Document, pdf_path: Path,
                    table_captions: list[dict]) -> list[dict]:
    try:
        import pdfplumber
        have_plumber = True
    except Exception:
        have_plumber = False
    plumber_pages = {}
    plumber_pdf = None
    if have_plumber:
        try:
            plumber_pdf = pdfplumber.open(str(pdf_path))
            plumber_pages = {p.page_number: p for p in plumber_pdf.pages}
        except Exception as exc:
            log.warning("pdfplumber open failed: %s", exc)
    settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines",
                "intersection_tolerance": 3}
    tables = []
    for cap in table_captions:
        page = doc[cap["page"] - 1]
        ruled = _ruled_table_region(page, cap)
        grid = _word_grid_table(page, cap, region=ruled)
        candidates = []
        if grid and max(len(r) for r in grid) >= 2:
            candidates.append(grid)
        pg = plumber_pages.get(cap["page"])
        anchor = ruled or cap["bbox"]
        anchor_cy = (anchor[1] + anchor[3]) / 2
        if pg is not None:
            for s in (settings, {"vertical_strategy": "text", "horizontal_strategy": "text"}):
                try:
                    for tbl in pg.find_tables(table_settings=s):
                        tb = tbl.bbox
                        if ruled:
                            # must be contained inside the ruled region; a
                            # whole-page text table is never the same table
                            if not (tb[0] >= ruled[0] - 15 and tb[2] <= ruled[2] + 15
                                    and tb[1] >= ruled[1] - 10
                                    and tb[3] <= ruled[3] + 10):
                                continue
                        elif abs(((tb[1] + tb[3]) / 2) - anchor_cy) > 180:
                            continue
                        raw = tbl.extract()
                        rows = [[(c or "").strip() for c in row] for row in raw]
                        rows = [r for r in rows if any(r)]
                        if rows and max(len(r) for r in rows) >= 2:
                            candidates.append(rows)
                except Exception as exc:
                    log.warning("pdfplumber failed p%s: %s", cap["page"], exc)
        if candidates:
            rows = max(candidates, key=lambda r: len(r) * max(len(x) for x in r))
        else:
            rows = grid or []
        tables.append({
            "id": cap["id"], "page": cap["page"], "caption": cap["text"],
            "bbox": cap["bbox"], "rows": rows, "region": ruled,
            "extracted": bool(rows) and max((len(r) for r in rows), default=1) >= 2,
        })
    if plumber_pdf is not None:
        plumber_pdf.close()
    return tables


def _best_table(found: list, cap: dict) -> list[list[str]] | None:
    cap_y = (cap["bbox"][1] + cap["bbox"][3]) / 2
    best, best_score = None, None
    for rows in found:
        clean = [[(c or "").strip() for c in row] for row in rows]
        clean = [r for r in clean if any(r)]
        if not clean:
            continue
        score = len(clean) * max(len(r) for r in clean)
        if best_score is None or score > best_score:
            best, best_score = clean, score
    return best


# ---------------------------------------------------------------------------
# Abstract / title / authors
# ---------------------------------------------------------------------------
def _extract_abstract(page_texts: list[str], doc: "fitz.Document | None" = None) -> str:
    joined = "\n".join(page_texts[:3])
    # Case-sensitive structural boundaries (re.I would let ordinary prose
    # match the all-caps/footnote/footer patterns); section words use inline (?i:).
    boundary = (
        r"\n\s*(?i:(?:1\s+)?Introduction)\b"
        r"|(?i:\bKeywords?\b)"
        r"|\n\s*[A-Z][A-Z ]{6,}\n"            # all-caps heading line
        r"|\n\s*[∗*†‡§]\s*"                   # contribution footnote marker
        r"|\n[^\n]{0,60}\bDOI\b[^\n]{0,60}"   # DOI / journal line
        r"|\n\s*\d+\s*/\s*\d+\s*\n"           # page footer, e.g. "1 / 16"
        r"|\n\s*arXiv:\S"                     # arXiv stamp
        r"|\n\s*(?i:author summary)\b"
        r"|\n\s*(?i:competing interests)\b"
        r"|$"
    )
    m = re.search(r"(?i:\bAbstract\b)\s*(.*?)(?:" + boundary + r")", joined, re.S)
    if m and len(m.group(1).strip()) > 80:
        return normalize_text(m.group(1))
    zm = re.search(r"摘\s*要\s*(.*?)(?:关键词|关 键 词|Abstract)", joined, re.S)
    if zm and len(zm.group(1).strip()) > 40:
        return normalize_text(zm.group(1))
    # fallback: first page paragraph after the word Abstract, minus author noise
    first = page_texts[0] if page_texts else ""
    idx = first.lower().find("abstract")
    if idx >= 0:
        tail = first[idx + 8:]
        paras = [p.strip() for p in re.split(r"\n\s*\n", tail) if len(p.strip()) > 100]
        if paras:
            return normalize_text(paras[0])
    # unlabeled abstract (e.g. Nature-family layout): first long, prose-like
    # block on the opening pages, before any DOI / URL
    if doc is not None:
        for pno in range(min(2, len(doc))):
            for block in _page_blocks(doc[pno]):
                text = normalize_text(" ".join(l["text"] for l in block["lines"]))
                if (len(text) > 350 and text.count(". ") >= 2
                        and not re.search(r"@|https?://|doi", text)
                        and not _GENERIC_RE.search(text)):
                    return text
    return ""


_BAD_TITLE_RE = re.compile(
    r"arxiv:|arxiv\.org|microsoft word|untitled|doi|http|proc\.|copyright|placeholder|"
    r"\[\w+\.\w+\]|\d{4}\s*$", re.I)
_NAME_LINE_RE = re.compile(
    r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+){0,3}[\*∗†‡§¶]*$")
_ORG_RE = re.compile(
    r"\b(brain|research|university|institute|laborator(?:y|ies)|labs?|department|cent(?:er|re)|"
    r"college|school|corporation|corp|inc|gmbh|hospital|academy|faculty|division|group|team|"
    r"chemistry|sciences?|therapeutics?|discovery|exploratory|platform|biosciences?|pharma|"
    r"oncology|google|microsoft|meta|amazon|nvidia|ibm|openai|deepmind|stanford|mit|berkeley)\b",
    re.I)
_GENERIC_RE = re.compile(
    r"open access|research article|original research|review article|case report|short communication|"
    r"editorial|^article$|^communication$|^letter$|^articles?$|document version|the authors|"
    r"publisher'?s? pdf|plos one|published in|general rights|publisher rights|take down|"
    r"research portal|link to publication|file download|how to cite|citation:", re.I)
_NAME_RUN_RE = re.compile(
    r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-\.]+(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-\.]+){1,3}")
_SURNAME_INITIAL_RE = re.compile(r"([A-Z][A-Za-z'\-]+),\s+((?:[A-Z]\.\s*){1,4})")


def _extract_names(spans: list[tuple], title_size: float, title: str) -> list[str]:
    names: list[str] = []
    title_words = set(re.findall(r"[A-Za-z\-]+", title.lower()))

    def add(cand: str) -> None:
        cand = cand.strip().rstrip(".")
        if not cand or cand in names:
            return
        if _ORG_RE.search(cand) or re.search(r"\b[A-Z]{2,}\b", cand):
            return  # affiliation lines / state codes
        words = re.findall(r"[A-Za-z\-]+", cand.lower())
        if words and all(w in title_words for w in words):
            return  # repeated title text, not a name
        names.append(cand)

    for size, text, _ in spans:
        if size >= title_size - 1.0:
            continue  # title lines are not author names
        if len(text) > 300:
            break     # abstract begins; everything below is body/affiliations
        raw = text.strip()
        if _GENERIC_RE.search(raw):
            continue
        surname_hits = list(_SURNAME_INITIAL_RE.finditer(raw))
        for m in surname_hits:  # Cameron, S. J. S., ...
            add(m.group(1))
        if surname_hits:
            continue  # citation line: do not fragment initials into name runs
        cleaned = re.sub(r"[\d,✉&∗†‡§¶]", " ", raw)
        for m in _NAME_RUN_RE.finditer(cleaned):
            add(m.group())
    return names


def _title_authors(doc: fitz.Document) -> tuple[str, str]:
    meta = doc.metadata or {}
    meta_title = (meta.get("title") or "").strip()
    page = doc[0]
    spans = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", []))
            size = max((s.get("size", 0) for s in line.get("spans", [])), default=0)
            if text.strip():
                spans.append((size, text.strip(), tuple(line["bbox"])))
    # visual title ignores stamp-like lines even when they use the largest font
    candidate_spans = [s for s in spans if not _BAD_TITLE_RE.search(s[1])]
    pool = candidate_spans or spans
    max_size = max((s[0] for s in pool), default=0)
    title_lines = [t for size, t, _ in pool if size >= max_size - 0.8][:3]
    visual_title = normalize_text(" ".join(title_lines))
    if meta_title and not _BAD_TITLE_RE.search(meta_title) and len(meta_title) >= 6:
        title = meta_title
    else:
        title = visual_title
    authors = truncate_words(", ".join(_extract_names(spans, max_size, visual_title)), 280)
    return title, authors


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def _ocr_page(pdf_path: Path, page_no: int, lang: str, work: Path) -> str:
    if not which("tesseract"):
        return ""
    pix_dir = ensure_dir(work / "ocr_tmp")
    doc = fitz.open(str(pdf_path))
    pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(2.6, 2.6), alpha=False)
    img_path = pix_dir / f"p{page_no:02d}.png"
    pix.save(str(img_path))
    code, out, err = run_cmd(["tesseract", str(img_path), "stdout", "-l", lang], timeout=180)
    return out if code == 0 else ""


# ---------------------------------------------------------------------------
# Review digest
# ---------------------------------------------------------------------------
def _build_review(paper: dict, page_texts: list[str]) -> str:
    lines = ["# Paper review (planning digest)", "",
             f"## Title\n{paper['title']}", "",
             f"## Authors\n{paper['authors'] or '(not detected)'}", ""]
    if paper["toc"]:
        lines.append("## Table of contents")
        for lvl, title, page in paper["toc"][:40]:
            lines.append(f"{'  ' * (lvl - 1)}- {title} (p.{page})")
        lines.append("")
    lines.append(f"## Abstract (verbatim)\n{paper['abstract'] or '(not detected)'}")
    lines.append("")
    lines.append("## Figures and tables (verbatim captions)")
    for cap in paper["captions"]:
        lines.append(f"- {cap['id']} | p.{cap['page']} | {cap['text']}")
    lines.append("")
    lines.append("## In-text mentions")
    for m in paper["mentions"][:30]:
        lines.append(f"- {m['id']} (p.{m['page']}): {m['context']}")
    lines.append("")
    lines.append("## Key numbers / units (with context)")
    for entry in paper["number_index"][:40]:
        lines.append(f"- p.{entry['page']} | {entry['token']} | {entry['context']}")
    lines.append("")
    lines.append("## Page text")
    for i, text in enumerate(page_texts, 1):
        clipped = text if len(text) <= 4500 else text[:4500] + " …[truncated]"
        lines.append(f"### Page {i}\n{clipped}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------
def extract_pdf(pdf_path: Path, work_dir: Path, language_hint: str | None) -> dict:
    work_dir = ensure_dir(work_dir)
    assets = ensure_dir(work_dir / ASSETS_DIR)
    changes = ChangeLog(work_dir)
    doc = fitz.open(str(pdf_path))
    encrypted = bool(doc.needs_pass)
    if encrypted:
        if not doc.authenticate(""):
            paper = {
                "source_pdf": str(pdf_path), "encrypted": True, "readable": False,
                "page_count": doc.page_count, "error": "encrypted PDF, no password supplied",
            }
            write_json(work_dir / PAPER_JSON, paper)
            changes.add("extract", "blocked", "encrypted PDF cannot be read")
            return paper
    page_texts, captions, mentions = [], [], []
    number_index, scanned_pages = [], []
    for page in doc:
        text = page.get_text()
        page_texts.append(text)
        if len(text.strip()) < SCANNED_CHAR_THRESHOLD:
            scanned_pages.append(page.number + 1)
        captions.extend(_find_captions(page, page.number + 1))
        mentions.extend(_mentions(text, page.number + 1))
        for item in _number_index(text, page.number + 1):
            number_index.append({"page": page.number + 1, **item})

    language = language_hint
    if not language:
        cjk = sum(cjk_ratio(t) for t in page_texts) / max(1, len(page_texts))
        language = "zh" if cjk > 0.15 else "en"

    # OCR scanned pages (real OCR, only when tesseract exists)
    ocr_texts = {}
    if scanned_pages and which("tesseract"):
        lang = "chi_sim+eng" if language == "zh" else "eng"
        for pno in scanned_pages[:6]:
            ocr = _ocr_page(pdf_path, pno, lang, work_dir)
            if ocr.strip():
                ocr_texts[str(pno)] = ocr
                changes.add("extract", "ocr", f"page {pno} OCR {len(ocr)} chars")

    title, authors = _title_authors(doc)
    abstract = _extract_abstract(page_texts, doc)

    # Page-level assignment: each graphic cluster goes to its nearest caption
    page_regions: dict[int, dict[str, tuple]] = {}
    for pno in {c["page"] for c in captions}:
        page = doc[pno - 1]
        page_caps = [c for c in captions if c["page"] == pno]
        pr = page.rect
        page_regions[pno] = _pair_all_regions(
            page_caps, _graphic_rects(page), (pr.x0, pr.y0, pr.x1, pr.y1))

    figures = []
    for cap in captions:
        if cap["kind"] != "figure":
            continue
        page = doc[cap["page"] - 1]
        region = page_regions[cap["page"]].get(cap["id"])
        asset = None
        if region:
            name = f"{cap['id']}_p{cap['page']}.png"
            try:
                asset = _crop_region(page, region, assets / name)
                changes.add("extract", "crop", f"{cap['id']} region {region}")
            except Exception as exc:
                log.warning("crop failed for %s: %s", cap["id"], exc)
        if not asset:  # embedded raster fallback
            embeds = _embedded_images(doc, cap["page"], assets)
            if embeds:
                asset = embeds[0]
                changes.add("extract", "embedded-image", f"{cap['id']} -> {asset['file']}")
        if not asset:  # band crop fallback (dense vector figures / heatmaps)
            try:
                band = _band_region(page, cap)
                if band[3] - band[1] > 55:
                    asset = _crop_region(page, band, assets / f"{cap['id']}_p{cap['page']}_band.png")
                    changes.add("extract", "band-crop", f"{cap['id']} {band}")
            except Exception as exc:
                log.warning("band crop failed for %s: %s", cap["id"], exc)
        is_schematic = bool(SCHEMATIC_RE.search(cap["text"]))
        is_photo = bool(PHOTO_RE.search(cap["text"]))
        figures.append({
            "id": cap["id"], "number": cap["number"], "page": cap["page"],
            "caption": cap["text"], "bbox": cap["bbox"],
            "asset": asset,
            "figure_type": "photo" if is_photo else ("schematic" if is_schematic else "data"),
            "provenance": "cited_work" if CITED_RE.search(cap["text"]) else "own_result",
            "region": region,
        })

    table_captions = [c for c in captions if c["kind"] == "table"]
    tables = _extract_tables(doc, pdf_path, table_captions)

    paper = {
        "source_pdf": str(pdf_path),
        "page_count": doc.page_count,
        "encrypted": encrypted,
        "readable": True,
        "scanned": bool(scanned_pages),
        "scanned_pages": scanned_pages,
        "ocr_available": bool(which("tesseract")),
        "ocr_texts": ocr_texts,
        "metadata": doc.metadata or {},
        "title": title, "authors": authors,
        "language": language,
        "toc": doc.get_toc(simple=True),
        "abstract": abstract,
        "captions": captions,
        "figures": figures,
        "tables": tables,
        "mentions": mentions,
        "number_index": number_index,
        "page_text": page_texts,
    }
    write_json(work_dir / PAPER_JSON, paper)
    review = _build_review(paper, page_texts)
    (work_dir / PAPER_REVIEW).write_text(review, encoding="utf-8")
    changes.add("extract", "done",
                f"{len(figures)} figures, {len(tables)} tables, {doc.page_count} pages")
    log.info("extracted: %d pages, %d figures (%d assets), %d tables, lang=%s, scanned=%s",
             doc.page_count, len(figures),
             sum(1 for f in figures if f["asset"]), len(tables), language, scanned_pages)
    return paper


def run(work_dir: Path | None = None) -> dict:
    work_dir = Path(work_dir or WORK_DIR)
    brief = read_json(work_dir / CASE_BRIEF)
    pdfs = brief["roles"]["papers"]
    if not pdfs:
        raise FileNotFoundError("no paper PDF registered at intake")
    paper = extract_pdf(Path(pdfs[0]), work_dir, brief.get("language_hint"))
    return {"paper": str(work_dir / PAPER_JSON), "readable": paper.get("readable", True)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir")
    args = parser.parse_args()
    run(args.work_dir)


if __name__ == "__main__":
    main()
