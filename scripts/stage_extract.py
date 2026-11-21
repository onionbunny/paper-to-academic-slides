"""Stage 2 — scientific paper extraction.

Parses the actual PDF (no pre-extracted answer layer):
  * per-page text, TOC and metadata
  * figure/table captions (English and Chinese) with verbatim text
  * in-text citation mentions (Fig. 3 / 图 3 ...) with context
  * figure region crops at 200 DPI (raster images and vector drawings),
    tagged as schematic / data / photo and own-result / cited work
  * tables via pdfplumber (real rows, never re-typed numbers)

Outputs paper.json plus assets/ and a bounded paper_review.md for planning.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz  # PyMuPDF

from common import (
    CASE_BRIEF,
    PAPER_JSON,
    WORK_DIR,
    ChangeLog,
    dehyphenate,
    get_logger,
    has_cjk,
    read_json,
    truncate_words,
    write_json,
    ensure_dir,
    merge_rects,
    rects_overlap,
)

log = get_logger("extract")

# ---------------------------------------------------------------------------
# Caption / mention patterns
# ---------------------------------------------------------------------------
FIG_LABEL = r"(?:fig(?:ure)?\.?\s*([Ss]?\d+(?:\.\d+)?[A-Za-z]?)|图\s*([Ss]?\d+(?:\.\d+)?[A-Za-z]?))"
TBL_LABEL = r"(?:table\s*([Ss]?\d+(?:\.\d+)?[A-Za-z]?)|表\s*([Ss]?\d+(?:\.\d+)?[A-Za-z]?))"
CAPTION_FIG_RE = re.compile(r"^\s*" + FIG_LABEL, re.I)
CAPTION_TBL_RE = re.compile(r"^\s*" + TBL_LABEL, re.I)
MENTION_FIG_RE = re.compile(FIG_LABEL, re.I)
MENTION_TBL_RE = re.compile(TBL_LABEL, re.I)

# strict caption gate: capture the first character after the label number and
# decide from its case / punctuation. A lowercase verb = in-text sentence.
_NEXT_CHAR_RE = re.compile(
    r"^\s*(?:fig(?:ure)?|table)\.?\s*[Ss]?\d+(?:\.\d+)?[A-Za-z]?\s*([:.。：])?\s*(.)",
    re.I)
_LABEL_EOL_RE = re.compile(
    r"^\s*(?:fig(?:ure)?|table)\.?\s*[Ss]?\d+(?:\.\d+)?[A-Za-z]?\s*[:.。：]?\s*$",
    re.I)
# CJK: label starts the line and is not an in-text reference (如图/如表/见图/见表/参见)
_CJK_CAPTION_RE = re.compile(r"^\s*(?:图|表)\s*[Ss]?\d+(?:\.\d+)?[A-Za-z]?")
_CJK_MENTION_RE = re.compile(r"(?:如|见|参见|参考)\s*(?:图|表)")


def _is_caption_start(first_line: str) -> bool:
    if _LABEL_EOL_RE.match(first_line):
        return True
    m = _NEXT_CHAR_RE.match(first_line)
    if m:
        separator, ch = m.group(1), m.group(2)
        if separator or ch in ",，":
            return True
        if ch.isupper():
            return True
        if ch.islower():
            return False
    if _CJK_CAPTION_RE.match(first_line) and not _CJK_MENTION_RE.search(first_line[:6]):
        return True
    return False


SCHEMATIC_RE = re.compile(r"schematic|diagram|workflow|conceptual|illustration|示意|流程|概念|框架|illustrated", re.I)
CITED_RE = re.compile(r"et\s*al\.|reproduced|adapted|reprinted|with permission|来源|引用|转载|modified from|redrawn", re.I)

MIN_RECT_IN = 0.4  # skip decorative marks smaller than ~0.4 inch
CROP_DPI = 200
PAGE_DPI = 110


def _page_lines(page: fitz.Page) -> list[dict]:
    """Return text lines with bbox (in PDF points) and text."""
    data = page.get_text("dict")
    lines = []
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append({"text": text, "bbox": tuple(line["bbox"])})
    return lines


def _label_id(match: re.Match) -> str:
    groups = [g for g in match.groups() if g]
    return groups[0] if groups else ""


def _find_captions(page: fitz.Page, page_no: int) -> list[dict]:
    """Detect captions at text-block (paragraph) boundaries so body text is
    never absorbed into a caption."""
    blocks = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append({"text": text, "bbox": tuple(line["bbox"])})
        if lines:
            blocks.append(lines)
    captions = []
    i = 0
    while i < len(blocks):
        lines = blocks[i]
        first = lines[0]["text"]
        if not _is_caption_start(first):
            i += 1
            continue
        mf = CAPTION_FIG_RE.match(first)
        mt = CAPTION_TBL_RE.match(first)
        if not mf and not mt:
            i += 1
            continue
        match = mf or mt
        all_lines = list(lines)
        joined = " ".join(line["text"].strip() for line in all_lines)
        j = i + 1
        # merge continuation blocks until the caption ends with sentence punctuation
        while j < len(blocks) and not re.search(r"[.!?。！？]\s*$", joined):
            next_first = blocks[j][0]["text"]
            if CAPTION_FIG_RE.match(next_first) or CAPTION_TBL_RE.match(next_first):
                break
            all_lines.extend(blocks[j])
            joined = " ".join(line["text"].strip() for line in all_lines)
            j += 1
        x0 = min(line["bbox"][0] for line in all_lines)
        y0 = min(line["bbox"][1] for line in all_lines)
        x1 = max(line["bbox"][2] for line in all_lines)
        y1 = max(line["bbox"][3] for line in all_lines)
        captions.append(
            {
                "id": _label_id(match),
                "kind": "figure" if mf else "table",
                "page": page_no,
                "bbox": (x0, y0, x1, y1),
                "caption": re.sub(r"\s+", " ", joined).strip(),
            }
        )
        i = j
    return captions


def _graphic_rects(page: fitz.Page) -> tuple[list[tuple], list[tuple]]:
    """Return (image rects in inches, vector drawing rects in inches)."""
    images, vectors = [], []
    page_rect = page.rect
    for info in page.get_images(full=True):
        xref = info[0]
        for r in page.get_image_rects(xref) or []:
            if r.width > MIN_RECT_IN * 72 and r.height > MIN_RECT_IN * 72:
                images.append((r.x0 / 72, r.y0 / 72, r.x1 / 72, r.y1 / 72))
    for drawing in page.get_drawings():
        r = drawing.get("rect")
        if r and r.width > MIN_RECT_IN * 72 and r.height > MIN_RECT_IN * 72:
            # ignore full-page background fills
            if r.width < page_rect.width - 6 and r.height < page_rect.height - 6:
                vectors.append((r.x0 / 72, r.y0 / 72, r.x1 / 72, r.y1 / 72))
    return merge_rects(images), merge_rects(vectors)


def _pair_region(caption: dict, clusters: list[tuple], page_h: float) -> tuple | None:
    cap = tuple(v / 72 for v in caption["bbox"])
    above = [c for c in clusters if c[3] <= cap[1] + 0.35]
    if above:
        # nearest cluster above the caption with horizontal overlap, else nearest by distance
        overlapping = [c for c in above if rects_overlap((c[0], c[1], c[2], c[3]),
                                                         (cap[0], cap[1], cap[2], cap[3]))]
        pool = overlapping or above
        return max(pool, key=lambda c: c[3])
    # caption may sit above its figure (rare) or figure wraps the caption: take nearest cluster
    if clusters:
        return min(clusters, key=lambda c: abs((c[1] + c[3]) / 2 - (cap[1] + cap[3]) / 2))
    return None


def _crop_region(page: fitz.Page, region_in: tuple, out_path: Path, dpi: int = CROP_DPI) -> None:
    clip = fitz.Rect(region_in[0] * 72, region_in[1] * 72, region_in[2] * 72, region_in[3] * 72)
    clip = clip & page.rect
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    pix.save(str(out_path))


def _mentions(text: str, page_no: int, page_captions: list[dict]) -> list[dict]:
    out = []
    # character ranges occupied by captions on this page (self-matches inside
    # captions are not in-text references)
    caption_ranges = []
    for cap in page_captions:
        needle = cap["caption"][:40]
        pos = text.find(needle)
        if pos >= 0:
            caption_ranges.append((pos, pos + len(cap["caption"])))
    # sentence boundaries for extending the context window to full sentences
    boundaries = [m.end() for m in re.finditer(r"[.!?。！？；](?:\s|$)", text)]
    for regex, kind in ((MENTION_FIG_RE, "figure"), (MENTION_TBL_RE, "table")):
        for m in regex.finditer(text):
            if any(a <= m.start() <= b for a, b in caption_ranges):
                continue
            start = max(0, m.start() - 120)
            end = min(len(text), m.end() + 120)
            prior = [b for b in boundaries if b <= m.start()]
            if prior:
                start = prior[-1]
            nxt = [b for b in boundaries if b >= m.end()]
            if nxt:
                end = nxt[0]
            ctx = re.sub(r"\s+", " ", text[start:end]).strip(" .。")
            out.append({"id": _label_id(m), "kind": kind, "page": page_no, "context": ctx})
    return out


def _cluster_words_to_table(region_words):
    """Shared word -> table reconstruction: rows by top, cells by horizontal
    gaps, global column anchors, then prune and validate. Returns None when the
    structure is implausible, so callers never render uncertain cells."""
    if len(region_words) < 4:
        return None
    row_groups = []
    for w in sorted(region_words, key=lambda x: (x["top"], x["x0"])):
        for row in row_groups:
            if abs(row["top"] - w["top"]) <= 3:
                row["words"].append(w)
                break
        else:
            row_groups.append({"top": w["top"], "words": [w]})
    row_groups.sort(key=lambda r: r["top"])
    kept = []
    prev_bottom = None
    for r in row_groups:
        if prev_bottom is not None and r["top"] - prev_bottom > 12:
            break
        kept.append(r)
        prev_bottom = max(w["bottom"] for w in r["words"])
    row_groups = kept
    row_cells = []
    for r in row_groups:
        ws = sorted(r["words"], key=lambda x: x["x0"])
        cells = [{"x0": ws[0]["x0"], "x1": ws[0]["x1"], "texts": [ws[0]["text"]]}]
        for w in ws[1:]:
            if w["x0"] - cells[-1]["x1"] > 6:
                cells.append({"x0": w["x0"], "x1": w["x1"], "texts": [w["text"]]})
            else:
                cells[-1]["texts"].append(w["text"])
                cells[-1]["x1"] = w["x1"]
        row_cells.append(cells)
    groups: list[list[float]] = []
    for x in sorted(c["x0"] for cells in row_cells for c in cells):
        if not groups or x - groups[-1][-1] > 10:
            groups.append([x])
        else:
            groups[-1].append(x)
    anchors = [sum(g) / len(g) for g in groups]
    if len(anchors) < 2 or len(row_cells) < 2:
        return None
    rows = []
    for cells in row_cells:
        out = [""] * len(anchors)
        for c in cells:
            ci = min(range(len(anchors)), key=lambda i: abs(anchors[i] - c["x0"]))
            out[ci] = " ".join(c["texts"]).strip()
        rows.append(out)
    result = _prune_columns(rows)
    if result:
        flat = [c for r in result for c in r]
        empty_ratio = sum(1 for c in flat if not c.strip()) / max(1, len(flat))
        if len(result[0]) > 8 or len(result) > 25 or empty_ratio > 0.5:
            return None
    return result


def _word_dicts(page) -> list[dict]:
    """PyMuPDF words as pdfplumber-style dicts (PDF point units, top-left
    origin). PyMuPDF keeps visually adjacent words separate more reliably than
    pdfplumber.extract_words, which can over-merge (e.g. Latin binomials)."""
    out = []
    for tup in page.get_text("words"):
        x0, y0, x1, y1, txt = tup[:5]
        out.append({"x0": x0, "x1": x1, "top": y0, "bottom": y1, "text": txt})
    return out


def _borderless_table_from_words(page, cap: dict, all_caps: list[dict]) -> list[list[str]] | None:
    """Fallback for borderless tables: cluster words into rows (top) and split
    cells by horizontal gaps. ``page`` is a PyMuPDF page; coordinates are PDF
    point units shared with the caption bboxes."""
    words = _word_dicts(page)

    def inside_caption(w) -> bool:
        for c in all_caps:
            cx0, cy0, cx1, cy1 = c["bbox"]
            if w["x0"] >= cx0 - 2 and w["x1"] <= cx1 + 2 and w["top"] < cy1 and w["bottom"] > cy0:
                return True
        return False

    words = [w for w in words if not inside_caption(w)]
    cap_x0, cap_top, cap_x1, cap_bottom = cap["bbox"]
    # horizontal bound: keep words within the caption column (slightly expanded),
    # so body text in the adjacent two-column block is never absorbed
    words = [w for w in words
             if w["x0"] >= cap_x0 - 8 and w["x1"] <= cap_x1 + 45]

    def build(region_words):
        return _cluster_words_to_table(region_words)

    # tables may sit above or below their caption
    above = [w for w in words if w["bottom"] < cap_top - 2 and w["top"] > cap_top - 200]
    below = [w for w in words if w["top"] > cap_bottom + 2 and w["top"] < cap_bottom + 200]
    return build(above) or build(below)


def _prune_columns(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    keep = [j for j in range(ncol) if any(r[j].strip() for r in rows)]
    rows = [[r[j] for j in keep] for r in rows]
    # merge adjacent columns whose populated rows never overlap (split headers)
    merged = True
    while merged and len(rows[0]) > 1:
        merged = False
        ncol = len(rows[0])
        for j in range(ncol - 1):
            conflict = any(rows[i][j].strip() and rows[i][j + 1].strip()
                           for i in range(len(rows)))
            if not conflict:
                for i in range(len(rows)):
                    a, b = rows[i][j], rows[i][j + 1]
                    rows[i][j] = (a + " " + b).strip() if a and b else (a or b)
                rows = [r[:j + 1] + r[j + 2:] for r in rows]
                merged = True
                break
    return rows


def _nearest_caption(tbl_bbox: tuple, caps: list[dict]) -> dict | None:
    """Pair a pdfplumber table bbox (PDF points, top-left origin) with its
    caption by vertical proximity and horizontal overlap."""
    if not caps:
        return None
    tx0, ttop, tx1, tbot = tbl_bbox

    def score(cap: dict):
        cx0, cy0, cx1, cy1 = cap["bbox"]
        horiz_overlap = max(0.0, min(tx1, cx1) - max(tx0, cx0))
        # vertical gap: caption above (cy0 >= tbot) or below (cy1 <= ttop)
        if cy0 >= tbot - 2:
            gap = cy0 - tbot
        elif cy1 <= ttop + 2:
            gap = ttop - cy1
        else:
            gap = 0.0
        return (horiz_overlap > 1.0, -gap)

    best = max(caps, key=score)
    bx0, btop0, bx1, bbot0 = best["bbox"]
    # reject if the caption is far away or in a different column
    horiz_overlap = max(0.0, min(tx1, bx1) - max(tx0, bx0))
    vertical_gap = max(0.0, btop0 - tbot, ttop - bbot0)
    if horiz_overlap < 1.0 or vertical_gap > 120:
        return None
    return best


def _rebuild_in_bbox(pdf_path: str, page_no: int, bbox: tuple,
                     cap: dict, all_caps: list[dict]) -> list[list[str]] | None:
    """Reconstruct a degenerate pdfplumber table from PyMuPDF words restricted
    to the detected table bbox (accurate region), then validate. Returns None
    unless the result is compact and dense."""
    x0, top, x1, bottom = bbox
    doc = fitz.open(pdf_path)
    page = doc[page_no - 1]
    words = _word_dicts(page)
    doc.close()

    def inside_caption(w):
        for c in all_caps:
            cx0, cy0, cx1, cy1 = c["bbox"]
            if w["x0"] >= cx0 - 2 and w["x1"] <= cx1 + 2 and w["top"] < cy1 and w["bottom"] > cy0:
                return True
        return False

    region = [w for w in words
              if not inside_caption(w)
              and w["x0"] >= x0 - 3 and w["x1"] <= x1 + 3
              and w["top"] >= top - 3 and w["bottom"] <= bottom + 3]
    return _cluster_words_to_table(region)


def _extract_tables_pdfplumber(pdf_path: str, table_captions: list[dict],
                               all_captions: list[dict],
                               password: str | None = None) -> list[dict]:
    tables = []
    try:
        import pdfplumber
    except Exception as exc:  # pragma: no cover
        log.warning("pdfplumber unavailable: %s", exc)
        return tables
    with pdfplumber.open(pdf_path, password=password or "") as pdf:
        fitz_doc = fitz.open(pdf_path)
        if password:
            fitz_doc.authenticate(password)
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                found = page.find_tables() or []
            except Exception as exc:
                log.warning("table extraction failed on page %d: %s", page_no, exc)
                found = []
            caps = [c for c in table_captions if c["page"] == page_no]
            paired_ids: set[str] = set()
            for tbl in found:
                try:
                    rows = tbl.extract() or []
                except Exception:
                    rows = []
                clean_rows = [[(cell or "").strip() for cell in row] for row in rows]
                clean_rows = [r for r in clean_rows if any(r)]
                if not clean_rows:
                    continue
                # choose the caption whose horizontal span best overlaps this table
                cap = _nearest_caption(tbl.bbox, caps)
                if cap is None or cap["id"] in paired_ids:
                    # caption-less or duplicate detection = ghost table; reject
                    continue
                n_cols = max(len(r) for r in clean_rows)
                # degenerate detection: collapsed to one column, or cells packed
                # with embedded newlines -> fall back to word clustering
                max_newlines = max(c.count("\n") for r in clean_rows for c in r)
                if n_cols < 2 or max_newlines >= 3:
                    # try word reconstruction restricted to the accurate bbox
                    rebuilt = _rebuild_in_bbox(str(pdf_path), page_no, tbl.bbox,
                                               cap, all_captions)
                    if rebuilt:
                        paired_ids.add(cap["id"])
                        tables.append({
                            "kind": "table", "id": cap["id"], "page": page_no,
                            "caption": cap["caption"], "rows": rebuilt,
                            "row_count": len(rebuilt),
                            "col_count": len(rebuilt[0]),
                            "extraction": "bbox word rebuild",
                        })
                    continue
                paired_ids.add(cap["id"])
                tables.append(
                    {
                        "kind": "table",
                        "id": cap["id"],
                        "page": page_no,
                        "caption": cap["caption"],
                        "rows": clean_rows,
                        "row_count": len(clean_rows),
                        "col_count": n_cols,
                    }
                )
            # borderless fallback for captions without a detected table
            have_ids = {t["id"] for t in tables if t["page"] == page_no}
            page_caps = [c for c in all_captions if c["page"] == page_no]
            for cap in caps:
                if cap["id"] in have_ids:
                    continue
                fallback_rows = _borderless_table_from_words(
                    fitz_doc[page_no - 1], cap, page_caps)
                if fallback_rows:
                    tables.append(
                        {
                            "kind": "table",
                            "id": cap["id"],
                            "page": page_no,
                            "caption": cap["caption"],
                            "rows": fallback_rows,
                            "row_count": len(fallback_rows),
                            "col_count": max(len(r) for r in fallback_rows),
                            "extraction": "word-cluster fallback",
                        }
                    )
        fitz_doc.close()
    return tables


_BOILERPLATE_RE = re.compile(
    r"arxiv|preprint|copyright|©|permission to make|provided proper attribution|"
    r"accepted|published|http|doi|license|licensed|all rights reserved", re.I)


_AUTHOR_BADGE_RE = re.compile(
    r"^(open access|publisher rights|general rights|take down policy|published in|"
    r"document version|download date|publisher|copyright|general rights|this research|"
    r"this is|link to|queen|http|https|published)", re.I)
# citation-style author: "Surname, X. X." (repository cover pages)
_CITE_NAME_RE = re.compile(
    r"([A-Z][A-Za-z\u00C0-\u017F'’\-]+),\s+((?:[A-Z]\.\s*)+),?")
_YEAR_PAREN_RE = re.compile(r"\((?:19|20)\d{2}\)")


def _authors_from_first_page(doc) -> str:
    """Collect person-name lines between the title and the abstract, skipping
    emails, affiliations, footnote markers and repository cover-page badges."""
    page = doc[0]
    cleaned_lines = []
    for b in page.get_text("dict")["blocks"]:
        if "lines" not in b:
            continue
        for l in b["lines"]:
            txt = "".join(s["text"] for s in l["spans"]).strip()
            if not txt:
                continue
            if re.search(r"abstract|摘要", txt):
                break
            low = txt.lower()
            if "@" in txt or re.search(r"http|arxiv|university|institute|laboratory|"
                                       r"google|brain|research|department|school|大学|学院|"
                                       r"研究所|医院", low):
                continue
            clean = re.sub(r"[∗†‡§¶*†‡§]+", "", txt).strip()
            if not clean or _AUTHOR_BADGE_RE.match(clean):
                continue
            cleaned_lines.append(clean)

    # Repository cover pages list authors in citation form: "Surname, X. X., ..."
    joined = " ".join(cleaned_lines)
    if _CITE_NAME_RE.search(joined):
        pre_year = _YEAR_PAREN_RE.split(joined)[0]
        names = []
        for m in _CITE_NAME_RE.finditer(pre_year):
            nm = f"{m.group(2).strip()} {m.group(1)}"
            if nm not in names:
                names.append(nm)
        if names:
            return ", ".join(names)

    # Standard form: "First Last" capitalized person-name lines
    names = []
    for clean in cleaned_lines:
        if re.fullmatch(r"[A-Z\u00C0-\u017F][A-Za-z\u00C0-\u017F'’\-]+(?:\s+(?:[A-Z\u00C0-\u017F]\.|[A-Z\u00C0-\u017F][A-Za-z\u00C0-\u017F'’\-]+)){1,3}",
                        clean):
            if clean not in names:
                names.append(clean)
    return ", ".join(names)


def _title_from_first_page(doc) -> str:
    """Largest-font line(s) on page 1, skipping banners/copyright blocks."""
    page = doc[0]
    lines = []
    for b in page.get_text("dict")["blocks"]:
        if "lines" not in b:
            continue
        for l in b["lines"]:
            txt = "".join(s["text"] for s in l["spans"]).strip()
            if not txt or _BOILERPLATE_RE.search(txt):
                continue
            size = max(s["size"] for s in l["spans"])
            lines.append((size, l["bbox"][1], txt))
    if not lines:
        return ""
    max_size = max(s for s, _, _ in lines)
    if max_size < 11:
        return ""
    title_lines = [t for s, _, t in lines if s >= max_size - 0.8]
    return " ".join(title_lines).strip()


def _build_review_md(paper: dict, pages_text: list[str]) -> str:
    lines = ["# Paper review (planning digest)", ""]
    lines.append(f"## Title\n{paper.get('title') or '(title not detected)'}")
    lines.append(f"## Authors\n{paper.get('authors') or '(not detected)'}")
    if paper.get("toc"):
        lines.append("## Table of contents")
        for lvl, title, page in paper["toc"][:40]:
            lines.append(f"{'  ' * (lvl - 1)}- {title} (p.{page})")
    abstract = paper.get("abstract")
    if abstract:
        lines.append("## Abstract (verbatim)")
        lines.append(truncate_words(abstract, 2200))
    lines.append("## Figures and tables (verbatim captions)")
    for item in paper["figures"] + paper["tables"]:
        cap = truncate_words(item.get("caption", ""), 500)
        lines.append(f"- {item['kind'][:3]} {item['id']} | p.{item['page']} | {cap}")
    lines.append("## Page text")
    for i, text in enumerate(pages_text, start=1):
        lines.append(f"### Page {i}")
        lines.append(truncate_words(text, 1800))
    return "\n".join(lines)[:48000]


def _trim_white_margins(image_path: Path, out_path: Path, pad: int = 12) -> Path | None:
    """Crop to the non-white bounding box so scanned content fills the frame."""
    try:
        from PIL import Image, ImageChops

        with Image.open(image_path).convert("RGB") as im:
            bg = Image.new("RGB", im.size, (255, 255, 255))
            diff = ImageChops.difference(im, bg)
            bbox = diff.getbbox()
            if not bbox:
                return None
            left = max(0, bbox[0] - pad)
            top = max(0, bbox[1] - pad)
            right = min(im.size[0], bbox[2] + pad)
            bottom = min(im.size[1], bbox[3] + pad)
            im.crop((left, top, right, bottom)).save(str(out_path))
        return out_path
    except Exception as exc:
        log.warning("trim failed for %s: %s", image_path, exc)
        return None


def _normalize_ocr(out: str) -> str:
    """Collapse horizontal whitespace but preserve line breaks so downstream
    title/author/abstract parsing keeps the reading structure."""
    out = re.sub(r"[^\S\n]+", " ", out)
    out = re.sub(r" ?\n ?", "\n", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()


def _run_ocr(image_path: Path, lang: str = "eng") -> str:
    """Run tesseract if installed; return OCR text (may contain errors)."""
    import shutil
    import subprocess

    if not shutil.which("tesseract"):
        return ""
    try:
        proc = subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", lang],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            return _normalize_ocr(proc.stdout)
        log.warning("tesseract failed (lang=%s): %s", lang, proc.stderr[:200])
        if lang != "eng":
            proc = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "eng"],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0:
                return _normalize_ocr(proc.stdout)
    except Exception as exc:
        log.warning("OCR error: %s", exc)
    return ""


def _readable_ratio(text: str) -> float:
    """Share of alphanumeric/CJK characters among non-whitespace. Real text
    (Latin or CJK) scores ~0.85+; PDFs with broken ToUnicode maps emit a
    punctuation/symbol soup that scores ~0.4-0.6."""
    nonspace = [c for c in text if not c.isspace()]
    if not nonspace:
        return 1.0
    return sum(1 for c in nonspace if c.isalnum()) / len(nonspace)


def _title_from_ocr(ocr_text: str) -> str:
    """Pick the first substantive Chinese/English title line from OCR text,
    skipping journal headers and joining an em-dash subtitle line."""
    header_re = re.compile(r"(卷|第.*期|Vol\.?|JOURNAL|DOI|DOL|MEAS|文章编号|CNKI|"
                           r"enki|ISSN|Oct|Nov|Nat\.?\s*Sci|20\d{2}\s*年|\d{1,2}\s*月)",
                          re.I)
    is_zh = bool(re.search(r"[\u4e00-\u9fff]", ocr_text))
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    cand = []
    for ln in lines:
        if not cand:
            if header_re.search(ln) or not re.search(r"[\u4e00-\u9fffA-Za-z]", ln):
                continue
            # a Chinese title line must contain a CJK ideograph
            if is_zh and not re.search(r"[\u4e00-\u9fff]", ln):
                continue
        if re.search(r"摘\s*要|Abstract|关键词|Key\s?words|学院|大学|研究中心|出版社", ln):
            if cand:
                break
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", ln):
            if cand:
                break
            continue
        cand.append(ln)
        if len(cand) >= 2 and not re.match(r"^[—–一\-－]+", ln):
            break
        if len(" ".join(cand)) >= 40:
            break
    title = " ".join(cand)
    title = re.sub(r"^[一－\-]+", "——", title)
    # OCR em-dashes: dash+一, doubled 一, or space-surrounded 一 (never a CJK word)
    title = re.sub(r"\s*[-－]\s*[一]{1,2}\s*", "——", title)
    title = re.sub(r"\s+[一]{1,2}(?=\s)", "——", title)
    title = re.sub(r"——+", "——", title)
    return title.strip()


def _authors_from_ocr(ocr_text: str) -> str:
    """Return an author line only when OCR yields clean CJK person names;
    otherwise empty (honest omission beats OCR garbage)."""
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    for i, ln in enumerate(lines):
        if re.search(r"摘\s*要|Abstract", ln):
            break
        # affiliation/address lines carry institution markers or postcodes;
        # journal-name lines carry 学报/期刊 and are never authors
        if re.search(r"学院|大学|研究中心|系|北京|上海|学报|期刊|出版社|\d{6}", ln):
            continue
        names = re.findall(r"[一-鿿]{2,4}(?=[，,、\s]|$)", ln)
        if len(names) >= 2 and not re.search(r"[A-Za-z0-9()（）]{3,}", ln):
            return "，".join(dict.fromkeys(names))
    return ""


def extract_pdf(pdf_path: Path, work_dir: Path, password: str | None = None) -> dict:
    assets_dir = ensure_dir(work_dir / "assets" / "figures")
    page_dir = ensure_dir(work_dir / "assets" / "pages")
    doc = fitz.open(str(pdf_path))
    encrypted = bool(doc.needs_pass)
    if encrypted:
        if doc.authenticate(""):
            pass
        elif password and doc.authenticate(password):
            ChangeLog(work_dir).add("extract", "authenticate", "opened with case-provided password")
        else:
            doc.close()
            raise PermissionError("encrypted PDF: password rejected")
    meta = doc.metadata or {}
    toc = doc.get_toc(simple=True)

    pages_text = []
    all_captions = []
    all_mentions = []
    figure_records = []
    used_regions: set[int] = set()
    scanned_page_nos: list[int] = []
    ocr_texts: dict[str, str] = {}
    page_graphics: dict[int, tuple[list, list]] = {}

    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_no = page_index + 1
        text = dehyphenate(page.get_text("text"))
        pages_text.append(text)
        lines = _page_lines(page)
        captions = _find_captions(page, page_no)
        all_captions.extend(captions)
        all_mentions.extend(_mentions(text, page_no, captions))

        img_rects, vec_rects = _graphic_rects(page)
        clusters = merge_rects(img_rects + vec_rects)
        page_graphics[page_no] = (clusters, img_rects)
        # full-page fallback render
        page_pix = page.get_pixmap(matrix=fitz.Matrix(PAGE_DPI / 72, PAGE_DPI / 72), alpha=False)
        page_pix.save(str(page_dir / f"page_{page_no:03d}.png"))

        # scanned / no text layer detection
        page_area_in = (page.rect.width / 72) * (page.rect.height / 72)
        img_area_in = sum(abs(fitz.Rect(r)) for r in merge_rects(img_rects))
        img_coverage = img_area_in / page_area_in if page_area_in else 0.0
        if len(text.strip()) < 30 and img_coverage >= 0.25:
            scanned_page_nos.append(page_no)
            ocr_texts[str(page_no)] = _run_ocr(page_dir / f"page_{page_no:03d}.png",
                                               lang="chi_sim+eng" if has_cjk(text) else "eng")
        elif len(text.strip()) >= 100 and _readable_ratio(text) < 0.75:
            # extractable but encoded (broken ToUnicode): the text layer is
            # symbol soup, so recover the real wording via OCR
            ChangeLog(work_dir).add("extract", "garbled-text-ocr",
                                    f"page {page_no} readable ratio {_readable_ratio(text):.2f}")
            scanned_page_nos.append(page_no)
            ocr_texts[str(page_no)] = _run_ocr(page_dir / f"page_{page_no:03d}.png",
                                               lang="chi_sim+eng")

        for cap in [c for c in captions if c["kind"] == "figure"]:
            region = _pair_region(cap, clusters, page.rect.height / 72)
            asset_type = "vector"
            if region is None:
                # fallback: area above the caption within text margins
                c = cap["bbox"]
                region = (0.55, 0.55, page.rect.width / 72 - 0.55, c[1] / 72 - 0.08)
            else:
                if any(rects_overlap(region, ir) for ir in img_rects):
                    asset_type = "raster"
            out = assets_dir / f"fig_{cap['id'].replace('.', '_')}_p{page_no}.png"
            try:
                _crop_region(page, region, out)
                asset = str(out)
            except Exception as exc:
                log.warning("crop failed for fig %s: %s", cap["id"], exc)
                asset = str(page_dir / f"page_{page_no:03d}.png")
            caption_text = cap["caption"]
            figure_records.append(
                {
                    "id": cap["id"],
                    "kind": "figure",
                    "page": page_no,
                    "caption": caption_text,
                    "asset_path": asset,
                    "region_in": [round(v, 3) for v in region],
                    "asset_type": asset_type,
                    "is_schematic": bool(SCHEMATIC_RE.search(caption_text)),
                    "is_cited_work": bool(CITED_RE.search(caption_text)),
                }
            )

    scanned = len(scanned_page_nos) >= max(1, doc.page_count / 2)
    # substitute recovered OCR wording for garbled/scanned text layers so the
    # planner and title/author logic read real text instead of symbol soup
    for pno in scanned_page_nos:
        ocr = ocr_texts.get(str(pno), "").strip()
        if ocr:
            pages_text[pno - 1] = ocr
    existing_fig_pages = {f["page"] for f in figure_records}
    for page_no in scanned_page_nos:
        if page_no in existing_fig_pages:
            continue
        page_png = page_dir / f"page_{page_no:03d}.png"
        trimmed_png = _trim_white_margins(page_png, assets_dir / f"scan_p{page_no}_trimmed.png")
        figure_records.append(
            {
                "id": f"page-{page_no}",
                "kind": "figure",
                "page": page_no,
                "caption": "Scanned page without extractable text layer",
                "asset_path": str(trimmed_png or page_png),
                "region_in": [0, 0, round(doc[page_no - 1].rect.width / 72, 3),
                              round(doc[page_no - 1].rect.height / 72, 3)],
                "asset_type": "page_render",
                "is_schematic": False,
                "is_cited_work": False,
                "scanned_page": True,
                "ocr_text": ocr_texts.get(str(page_no), ""),
            }
        )

    # uncaptioned images (e.g. formula images): keep them as images, never retype
    for page_no, (clusters, img_rects) in page_graphics.items():
        page = doc[page_no - 1]
        paired = [f["region_in"] for f in figure_records if f["page"] == page_no]
        n = 0
        for cluster in clusters:
            if any(rects_overlap(cluster, region) for region in paired):
                continue
            if not any(rects_overlap(cluster, ir) for ir in img_rects):
                continue
            # require a meaningful image area
            cw, ch = cluster[2] - cluster[0], cluster[3] - cluster[1]
            if cw < 1.0 or ch < 0.5:
                continue
            # skip full-page regions (repository cover pages / page backgrounds):
            # a real figure never fills the whole page incl. margins
            pw_in, ph_in = page.rect.width / 72, page.rect.height / 72
            if cw >= 0.85 * pw_in and ch >= 0.85 * ph_in:
                continue
            n += 1
            label = f"uc-p{page_no}-{n}"
            out = assets_dir / f"uncap_{label}.png"
            try:
                _crop_region(page, cluster, out)
                asset = str(out)
            except Exception as exc:
                log.warning("crop failed for uncaptioned image: %s", exc)
                continue
            figure_records.append(
                {
                    "id": label,
                    "kind": "figure",
                    "page": page_no,
                    "caption": "Uncaptioned image in source (kept as image, not retyped)",
                    "asset_path": asset,
                    "region_in": [round(v, 3) for v in cluster],
                    "asset_type": "uncaptioned_image",
                    "is_schematic": False,
                    "is_cited_work": False,
                    "uncaptioned": True,
                }
            )

    # table captions + pdfplumber rows
    table_captions = [c for c in all_captions if c["kind"] == "table"]
    table_records = _extract_tables_pdfplumber(str(pdf_path), table_captions,
                                               all_captions, password)
    table_ids_with_rows = {t["id"] for t in table_records}
    for cap in table_captions:
        if cap["id"] not in table_ids_with_rows:
            table_records.append(
                {
                    "kind": "table",
                    "id": cap["id"],
                    "page": cap["page"],
                    "caption": cap["caption"],
                    "rows": [],
                    "row_count": 0,
                    "col_count": 0,
                }
            )

    first_page = pages_text[0] if pages_text else ""
    head_lines = [ln.strip() for ln in first_page.split("\n") if ln.strip()]
    bad_meta = {"", "anonymous", "untitled", "microsoft word", "document", "unknown"}

    def is_bad(value: str) -> bool:
        cleaned = re.sub(r"[()（）]", "", value or "").strip().lower()
        return cleaned in bad_meta

    title = (meta.get("title") or "").strip()
    ocr_first = ocr_texts.get("1", "").strip()
    if ocr_first and (not title or _readable_ratio(title) < 0.75):
        title = _title_from_ocr(ocr_first)
    if is_bad(title):
        # prefer the largest-font line on page 1 (robust to arXiv banners)
        title = _title_from_first_page(doc)
    if is_bad(title) and ocr_first:
        title = _title_from_ocr(ocr_first)
    if is_bad(title):
        # join wrapped title lines until a byline/abstract marker
        parts = []
        for ln in head_lines[:4]:
            if re.search(r",|@|abstract|university|大学|研究所", ln, re.I):
                break
            parts.append(ln)
            if len(" ".join(parts)) > 150:
                break
        title = " ".join(parts)
    title = truncate_words(title, 200)
    authors = (meta.get("author") or "").strip()
    if ocr_first and (not authors or _readable_ratio(authors) < 0.75):
        authors = _authors_from_ocr(ocr_first)
    if is_bad(authors):
        authors = _authors_from_first_page(doc)
    if authors and _readable_ratio(authors) < 0.75 and ocr_first:
        authors = _authors_from_ocr(ocr_first)
    if is_bad(authors):
        # heuristic: the short byline line near the top (contains a comma,
        # initials or a university marker)
        for ln in head_lines[1:6]:
            if re.search(r"学报|期刊|大学|学院|研究中心|出版社", ln):
                continue
            if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|"
                        r"^\d{4}\s*年|^\d{1,2}\s*月", ln, re.I):
                continue
            if len(ln) < 160 and ("," in ln or re.search(r"[A-Z]\.", ln) or
                                  "university" in ln.lower() or "大学" in ln or
                                  "研究所" in ln):
                authors = ln
                break
    abstract = ""
    m = re.search(r"(?is)\babstract\b\s*(.*?)(?:\n\s*(?:1\.?\s*)?introduction\b|\bkeywords?\b)", first_page)
    if m:
        abstract = re.sub(r"\s+", " ", m.group(1)).strip()
    else:
        mzh = re.search(r"摘\s*要[:：]?\s*(.*?)(?:\n\s*关键词|关\s*键\s*词)", first_page, re.S)
        if mzh:
            abstract = re.sub(r"\s+", " ", mzh.group(1)).strip()
        elif first_page:
            abstract = truncate_words(first_page, 1500)

    paper = {
        "source_pdf": str(pdf_path.resolve()),
        "page_count": doc.page_count,
        "encrypted": encrypted,
        "scanned": scanned,
        "scanned_pages": scanned_page_nos,
        "ocr_available": bool(ocr_texts),
        "ocr_texts": ocr_texts,
        "metadata": {k: v for k, v in meta.items() if v},
        "title": title.strip(),
        "authors": authors.strip(),
        "language": "zh" if has_cjk("".join(pages_text[:2])) else "en",
        "toc": [list(t) for t in toc],
        "abstract": abstract,
        "figures": sorted(figure_records, key=lambda x: (x["page"], x["id"])),
        "tables": sorted(table_records, key=lambda x: (x["page"], str(x["id"]))),
        "captions": all_captions,
        "mentions": all_mentions,
    }
    doc.close()
    write_json(work_dir / PAPER_JSON, paper)
    review_md = _build_review_md(paper, pages_text)
    (work_dir / "paper_review.md").write_text(review_md, encoding="utf-8")
    log.info("parsed %d pages, %d figures, %d tables, mentions=%d",
             paper["page_count"], len(paper["figures"]), len(paper["tables"]), len(all_mentions))
    return paper


def run(work_dir: Path) -> dict:
    brief = read_json(work_dir / CASE_BRIEF)
    changes = ChangeLog(work_dir)
    primary = brief.get("primary_paper")
    if not primary:
        raise FileNotFoundError("no paper PDF found in input directory")
    constraints = brief.get("constraints") or {}
    password = (constraints.get("pdf_password") or brief.get("password")
                or (brief.get("materials") or {}).get("password"))
    paper = extract_pdf(Path(primary), work_dir, password)
    changes.add("extract", "parse", f"{paper['page_count']} pages, {len(paper['figures'])} figures")
    return paper


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    run(Path(args.work_dir))


if __name__ == "__main__":
    main()
