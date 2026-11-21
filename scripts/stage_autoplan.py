"""Deterministic fallback planner.

Builds slide_plan.json directly from paper.json without model calls. It is the
safety net used when the executing agent cannot author a semantic plan, and the
baseline the agent is expected to improve. Also provides shared helpers for
loading verbatim page text and splitting sentences.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fitz

from common import (
    CASE_BRIEF,
    PAPER_JSON,
    SLIDE_PLAN,
    WORK_DIR,
    ChangeLog,
    dehyphenate,
    get_logger,
    has_cjk,
    read_json,
    truncate_words,
    write_json,
)

log = get_logger("autoplan")
HARD_DEFAULT_CAP = 20


def page_texts(paper: dict, password: str | None = None) -> list[str]:
    """Re-open the source PDF and return verbatim text per page."""
    doc = fitz.open(paper["source_pdf"])
    if doc.needs_pass and not doc.authenticate(""):
        if not password or not doc.authenticate(password):
            doc.close()
            raise PermissionError("encrypted PDF: password rejected")
    texts = [dehyphenate(doc[i].get_text("text")) for i in range(doc.page_count)]
    doc.close()
    return texts


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    if has_cjk(text):
        parts = re.split(r"(?<=[。！？；])", text)
    else:
        parts = re.split(r"(?<=[.!?;])\s+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _strip_label(caption: str) -> str:
    text = re.sub(r"^\s*(?:fig(?:ure)?\.?|table|图|表)\s*[Ss]?\d+(?:\.\d+)?[A-Za-z]?\.?\s*[:：.。]?\s*",
                  "", caption, flags=re.I).strip()
    return text.lstrip(":：.。,，;； \t").strip()


def _clean_title(caption: str, max_chars: int = 190) -> str:
    text = _strip_label(caption)
    if len(text) <= max_chars:
        return text
    for sep in (". ", "。", "; ", "；", ", ", "，", " "):
        pos = text.rfind(sep, 0, max_chars)
        if pos > max_chars * 0.55:
            return text[:pos].strip()
    return text[:max_chars].strip()


def _contexts_for(paper: dict, item_id: str, kind: str) -> list[str]:
    out = []
    for m in paper.get("mentions", []):
        if m["id"] == item_id and m["kind"] == kind:
            ctx = _clean_context(m["context"])
            if ctx and _is_complete_phrase(ctx) and ctx not in out:
                out.append(ctx)
    return out[:3]


_POINTER_RE = re.compile(
    r"^(?:we (?:present|show|summarize|give|report)\b.*(?:in|see)\s*"
    r"(?:table|fig(?:ure)?)\s*[Ss]?\d+|"
    r"(?:table|fig(?:ure)?)\s*[Ss]?\d+\s+(?:summarizes?|shows?|presents?|lists?|gives?)\b"
    r".*(?:literature|results?)\.?)$", re.I)
_SECTION_LEAD_RE = re.compile(r"^(?:\d+(?:\.\d+)*\s+[A-Z][A-Za-z &]+\s*)+")


def _truncate_phrase(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    for sep in ("; ", ", ", "，", "; ", "，", " "):
        pos = window.rfind(sep)
        if pos > limit * 0.6:
            return window[:pos].strip(" ,;，")
    return window.rstrip(" ,;，")


def _clean_context(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # drop leading numbered section headings (e.g. "6 Results 6.1 Machine Translation")
    text = _SECTION_LEAD_RE.sub("", text).strip()
    if _POINTER_RE.match(text):
        return ""
    return text


def _is_complete_phrase(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if has_cjk(text):
        return len(text) >= 6
    # reject fragments that begin with a lowercase verb or dangling punctuation
    if text[0] in ").,;:" or text[0].islower():
        return False
    return len(text) >= 12 and len(text.split()) >= 3


def _evidence_for(item: dict, page_text: str) -> dict:
    quote = truncate_words(page_text, 220)
    return {"kind": item["kind"], "id": item["id"], "page": item["page"],
            "section": None, "quote": quote}


def _caption_topic(item: dict, lang: str) -> str:
    """A short, item-specific topic from the caption (label stripped), used to
    make speaker notes on repeated templates distinguishable."""
    cap = _strip_label(item.get("caption", ""))
    if not cap:
        return ""
    if lang == "zh":
        chars = re.findall(r"[\u4e00-\u9fff]+", cap)
        topic = "".join(chars)[:14]
        return topic
    words = re.findall(r"[A-Za-z][A-Za-z\-]+", cap)
    return " ".join(words[:6])


def _evidence_notes(kind: str, item: dict, kind_idx: int, total: int,
                    has_context: bool, lang: str) -> str:
    label = f"{'Fig' if kind == 'figure' else 'Table'} {item['id']}"
    page = item["page"]
    if lang == "zh":
        if kind == "table":
            frames = [
                f"先定位第 {page} 页{label}，带听众读表头与关键一列，再点出对比方向，避免逐行念。",
                f"这张{label}（第 {page} 页）只讲结论：先给对比方向，再用一两个数值佐证，细节留给提问。",
                f"借助{label}过渡到结果：指出第 {page} 页最值得注意的行，说明它如何回应前面的问题。",
                f"围绕{label}做一次小结，提醒听众关注第 {page} 页的口径与单位，再继续下一张。",
                f"用提问带出{label}：先让大家在第 {page} 页找关键差异，再公布答案并解释原因。",
            ]
        elif item.get("is_schematic"):
            frames = [
                f"{label}是结构示意（第 {page} 页），按信息流向讲清模块关系，不纠缠图形细节。",
                f"用第 {page} 页这张{label}串起整体设计，强调它在论证链条中的位置。",
                f"面对{label}先给全局再讲局部，让听众记住各模块如何协作（原图见第 {page} 页）。",
                f"借{label}回顾术语：指着第 {page} 页的关键部件命名，为后续结果页铺垫。",
                f"以{label}为地图，说明输入如何一步步变成输出，必要时回到第 {page} 页。",
            ]
        else:
            frames = [
                f"让视线先落在第 {page} 页{label}的关键区域，一句话给出它支撑的结论，再补条件。",
                f"这张{label}（第 {page} 页）与上一张形成递进，先指变化再解释含义。",
                f"从{label}切入：描述图中现象，再落到它证明了什么，必要时翻回第 {page} 页核对。",
                f"提醒听众注意{label}里容易忽略的细节，再说明它如何强化第 {page} 页的论点。",
                f"用对比的方式讲{label}：先讲看图方法，再让结论自然浮现（原图第 {page} 页）。",
                f"围绕第 {page} 页{label}先给结论再回看证据，指出支撑这一结论的具体位置。",
                f"把{label}放进整体叙事：说明第 {page} 页这一步要回答什么问题，再点出答案。",
                f"用一句话概括{label}的要点，随后带听众在第 {page} 页核对图中的趋势与条件。",
            ]
        topic = _caption_topic(item, "zh")
        tail = f"（证据 {kind_idx + 1}/{total}）"
        if topic:
            tail = f"主题：{topic}。" + tail
        return frames[kind_idx % len(frames)] + tail
    if kind == "table":
        frames = [
            f"On page {page}, anchor {label} at the header and the key comparison column, then give the direction of effect instead of reading rows.",
            f"Use {label} (page {page}) for the bottom line: state the contrast first, support it with one or two values, and hold the rest for questions.",
            f"Transition to results through {label}: point to the most informative row on page {page} and tie it back to the opening question.",
            f"Use {label} as a quick checkpoint: note the units and setup on page {page} before moving on.",
            f"Pose a question around {label}, let the audience spot the difference on page {page}, then reveal the answer and its implication.",
        ]
    elif item.get("is_schematic"):
        frames = [
            f"{label} on page {page} is a schematic; follow the data flow and module roles rather than visual detail.",
            f"Use the {label} diagram (page {page}) to connect the whole design, emphasising where it sits in the argument.",
            f"Give the big picture of {label} first, then zoom into parts the audience needs to remember; the original is on page {page}.",
            f"Introduce terminology through {label}: name the key components on page {page} to set up the result slides.",
            f"Treat {label} as a map of how input becomes output, returning to page {page} if details are needed.",
        ]
    else:
        frames = [
            f"Point first to the key region of {label} on page {page}, give the one-sentence conclusion, then add the conditions.",
            f"{label} (page {page}) extends the previous point: name the change, then explain what it means.",
            f"Start from what {label} shows on page {page}, describe the pattern, then land on the claim it supports.",
            f"Draw attention to the easy-to-miss detail in {label} and show how it reinforces the argument on page {page}.",
            f"Teach how to read {label} first, then let the conclusion emerge; the original figure is on page {page}.",
            f"Give the conclusion from {label} first, then trace back to the exact spot on page {page} that supports it.",
            f"Place {label} in the story: state the question on page {page} it answers, then give the answer.",
            f"Summarise {label} in one line, then walk the audience through the trend and conditions shown on page {page}.",
        ]
    topic = _caption_topic(item, "en")
    tail = f"(evidence {kind_idx + 1}/{total})"
    if topic:
        tail = f"Topic: {topic}. " + tail
    return frames[kind_idx % len(frames)] + tail


def build_plan(work_dir: Path) -> dict:
    brief = read_json(work_dir / CASE_BRIEF)
    paper = read_json(work_dir / PAPER_JSON)
    constraints = brief.get("constraints", {})
    texts = page_texts(paper, constraints.get("pdf_password"))
    cap = constraints.get("max_slides") or HARD_DEFAULT_CAP
    lang = paper.get("language", "en")
    uncertainty: list[str] = []

    uncaptioned = [f for f in paper.get("figures", []) if f.get("uncaptioned")]
    for f in uncaptioned:
        uncertainty.append(
            f"uncaptioned image {f['id']} (p.{f['page']}): kept verbatim as image; no source label or caption exists")

    abstract_text = paper.get("abstract", "")
    if paper.get("scanned"):
        ocr_combined = " ".join(paper.get("ocr_texts", {}).values()).strip()
        if ocr_combined and not abstract_text:
            abstract_text = ocr_combined
            uncertainty.append("scanned PDF: content derived from OCR and may contain errors")
        uncertainty.append(
            "scanned PDF: text layer unavailable; evidence located at page level via rendered page images")
    abstract_sents = split_sentences(abstract_text)
    spine = {
        "problem": abstract_sents[0] if abstract_sents else truncate_words(paper.get("abstract", ""), 200),
        "gap": abstract_sents[1] if len(abstract_sents) > 1 else "",
        "method": next((s for s in abstract_sents if re.search(r"method|approach|propose|develop|we use|采用|方法|构建", s, re.I)), ""),
        "key_results": [s for s in abstract_sents
                        if re.search(r"\d|result|show|increase|decrease|reach|recover|sensitivity|improv|发现|结果|显著", s, re.I)
                        and not re.search(r"require|noisy|remain|需要|噪声", s, re.I)][:4],
        "contribution": abstract_sents[-1] if abstract_sents else "",
        "limitations": [],
    }

    deck_title = paper.get("title", "")
    if not deck_title and paper.get("scanned"):
        deck_title = "Scanned research document (title not recoverable from source)"

    slides: list[dict] = []
    # 1. title
    slides.append({
        "slide_no": 1, "role": "title", "layout_role": "title",
        "title": truncate_words(deck_title, 180),
        "claim": deck_title,
        "bullets": [{"text": paper.get("authors", ""), "level": 0}],
        "figures": [], "tables": [],
        "evidence": [{"kind": "page", "id": None, "page": 1, "section": None,
                      "quote": truncate_words(texts[0] if texts else "", 200)}],
        "notes": "Open the talk by stating the paper title and authors; one sentence on why this question matters to the audience.",
        "source_label": "p.1",
    })

    # 2. problem / background
    problem_bullets = [{"text": truncate_words(s, 160), "level": 0}
                       for s in abstract_sents[:2] if s][:3]
    if not problem_bullets:
        problem_bullets = [{"text": spine["problem"], "level": 0}]
    slides.append({
        "slide_no": 2, "role": "content", "layout_role": "content",
        "title": "研究背景与问题" if lang == "zh" else "Background and problem",
        "claim": spine["problem"],
        "bullets": problem_bullets, "figures": [], "tables": [],
        "evidence": [{"kind": "page", "id": None, "page": 1, "section": "Abstract",
                      "quote": truncate_words(paper.get("abstract", ""), 220)}],
        "notes": "Frame the gap before naming the solution; pause after the problem statement so the audience feels the motivation.",
        "source_label": "Abstract · p.1",
    })

    # 3. method overview
    method_sent = spine["method"] or (abstract_sents[2] if len(abstract_sents) > 2 else "")
    if method_sent:
        slides.append({
            "slide_no": 3, "role": "content", "layout_role": "content",
            "title": "核心方法" if lang == "zh" else "Core approach",
            "claim": truncate_words(method_sent, 160),
            "bullets": [{"text": truncate_words(method_sent, 180), "level": 0}],
            "figures": [], "tables": [],
            "evidence": [{"kind": "page", "id": None, "page": 1, "section": "Abstract",
                          "quote": truncate_words(method_sent, 220)}],
            "notes": "Explain the approach at the conceptual level first; details follow with the evidence slides.",
            "source_label": "Abstract · p.1",
        })

    # budget for evidence slides
    fixed = len(slides) + 1  # reserve conclusion
    evidence_cap = max(0, cap - fixed)
    items = [("figure", f) for f in paper.get("figures", [])]
    items += [("table", t) for t in paper.get("tables", [])]
    for t in paper.get("tables", []):
        if t.get("row_count", 0) == 0:
            uncertainty.append(
                f"table {t['id']} (p.{t['page']}) could not be reconstructed reliably; "
                "kept as a source reference rather than rendering uncertain cells")

    def _rank_key(entry: tuple) -> int:
        kind, item = entry
        if kind == "figure" and item.get("is_schematic"):
            return 2  # schematics are weakest standalone evidence
        if kind == "table":
            return 1
        return 0  # data figures first

    if len(items) > evidence_cap:
        # compression: prioritize data figures, then tables, schematics last
        ranked = sorted(items, key=_rank_key)
        if evidence_cap > 0:
            step = len(ranked) / evidence_cap
            chosen_idx = sorted({min(len(ranked) - 1, int(i * step)) for i in range(evidence_cap)})
            selected = [ranked[k] for k in chosen_idx]
        else:
            selected = []
        omitted = [it for it in items if it not in selected]
        labels = ", ".join(f"{k} {it['id']}" for k, it in omitted)
        uncertainty.append(f"{len(omitted)} evidence items omitted ({labels}) to respect slide cap {cap}")
    else:
        selected = items

    total_evidence = len(selected)
    kind_counter: dict[str, int] = {}
    for seq, (kind, item) in enumerate(selected, start=1):
        kind_counter[kind] = kind_counter.get(kind, 0) + 1
        kind_idx = kind_counter[kind] - 1
        page_text = texts[item["page"] - 1] if 0 < item["page"] <= len(texts) else ""
        contexts = _contexts_for(paper, item["id"], kind)
        # pages where the in-text discussion actually appears (may differ from
        # the item's own page), so every number stays traceable
        mention_pages = sorted({m["page"] for m in paper.get("mentions", [])
                                if m["id"] == item["id"] and m["kind"] == kind})
        evidence = [_evidence_for(item, page_text)]
        for mp in mention_pages:
            if mp == item["page"] or not (1 <= mp <= len(texts)):
                continue
            evidence.append({"kind": "page", "id": None, "page": mp,
                             "section": "in-text reference",
                             "quote": truncate_words(texts[mp - 1], 200)})
        # bullets only carry in-text discussion; never duplicate the caption
        bullets = [{"text": _truncate_phrase(c, 170), "level": 0} for c in contexts]
        if not bullets:
            if lang == "zh":
                fallback = ("按信息流向理解各模块如何连接" if item.get("is_schematic")
                            else "先定位关键趋势/区域，再结合图注中的条件与样本量")
            else:
                fallback = ("Follow the data flow to see how the components connect"
                            if item.get("is_schematic")
                            else "Locate the key trend or region, then use the caption's conditions and sample size")
            bullets = [{"text": fallback, "level": 0}]
        slide = {
            "slide_no": len(slides) + 1,
            "role": kind, "layout_role": "figure" if kind == "figure" else "content",
            "title": _clean_title(item.get("caption", "") or f"{kind} {item['id']}"),
            "claim": _clean_title(item.get("caption", ""), 220),
            "bullets": bullets,
            "figures": [], "tables": [],
            "evidence": evidence,
            "notes": _evidence_notes(kind, item, kind_idx, total_evidence,
                                     bool(contexts), lang),
            "source_label": f"{'Fig' if kind == 'figure' else 'Table'} {item['id']} · p.{item['page']}",
        }
        if kind == "figure":
            slide["figures"] = [{
                "id": item["id"], "kind": "figure",
                "asset_path": item["asset_path"],
                "caption_verbatim": item.get("caption", ""),
                "is_schematic": item.get("is_schematic", False),
                "is_cited_work": item.get("is_cited_work", False),
            }]
        else:
            if item.get("row_count", 0) > 0:
                slide["tables"] = [{"id": item["id"]}]
            else:
                # honest source-reference slide; no uncertain cells rendered
                cue = ("完整表格见源论文第 " + str(item["page"]) + " 页，本页仅作要点指引"
                       if lang == "zh"
                       else f"Full table on source page {item['page']}; refer to it directly for exact values")
                slide["bullets"] = [{"text": cue, "level": 0}]
                slide["layout_role"] = "content"
        slides.append(slide)

    # final conclusion — evidence spans the abstract (results originate there)
    # and the final discussion page so every number remains traceable
    result_sents = spine["key_results"] or abstract_sents[-2:]
    conclusion_evidence = [
        {"kind": "page", "id": None, "page": 1, "section": "Abstract",
         "quote": truncate_words(paper.get("abstract", ""), 220)},
        {"kind": "page", "id": None, "page": paper["page_count"],
         "section": "Conclusion",
         "quote": truncate_words(texts[-1] if texts else "", 220)},
    ]
    slides.append({
        "slide_no": len(slides) + 1, "role": "conclusion", "layout_role": "content",
        "title": "结论与贡献" if lang == "zh" else "Conclusions and contribution",
        "claim": spine["contribution"] or (result_sents[-1] if result_sents else ""),
        "bullets": [{"text": truncate_words(s, 170), "level": 0} for s in result_sents][:4]
                   or [{"text": truncate_words(spine["contribution"], 170), "level": 0}],
        "figures": [], "tables": [],
        "evidence": conclusion_evidence,
        "notes": "Land the take-home message explicitly, then name the contribution in one sentence; invite questions on limitations.",
        "source_label": f"Abstract p.1 · Conclusion p.{paper['page_count']}",
    })

    plan = {
        "version": "1.0",
        "case_id": brief.get("case_id"),
        "planner": "deterministic_fallback",
        "language": lang,
        "constraints": constraints,
        "spine": spine,
        "slides": slides,
        "uncertainty": uncertainty,
        "warnings": brief.get("warnings", []),
    }
    write_json(work_dir / SLIDE_PLAN, plan)
    changes = ChangeLog(work_dir)
    changes.add("planning", "autoplan", f"{len(slides)} slides within cap {cap}")
    log.info("fallback plan: %d slides, uncertainty=%d", len(slides), len(uncertainty))
    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=str(WORK_DIR))
    args = ap.parse_args()
    build_plan(Path(args.work_dir))


if __name__ == "__main__":
    main()
