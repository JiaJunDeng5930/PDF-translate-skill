# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import dataclass
from typing import Any

PLACEHOLDER_RE = re.compile(r"</?b\d+>", re.IGNORECASE)
ARROW_PLACEHOLDER_RE = re.compile(r"(?<=\w)\s*-\s*</?b\d+>\s*(?=\w)", re.IGNORECASE)
CID_TEXT_RE = re.compile(r"\(cid:(?P<code>\d+)\)")
CID_TEXT_REPLACEMENTS = {
    "82": "\u2020",
}
GLUED_WORD_REPAIRS = (
    ("thesemodelsoffer", "these models offer"),
    ("thesemodels", "these models"),
    ("thesechallenges", "these challenges"),
    ("unprecedentedspeed", "unprecedented speed"),
    ("promisingtruly", "promising truly"),
    ("introduceChordEdit", "introduce ChordEdit"),
    ("lowenergy", "low energy"),
    ("text-toimage", "text-to-image"),
    ("textguided", "text-guided"),
    ("textprompts", "text prompts"),
    ("editingfield", "editing field"),
    ("thefield", "the field"),
)
FIGURE_LABEL_LAYOUTS = {
    "figure",
    "figure_text",
    "figure_title",
    "figure_caption",
    "figure caption",
    "caption",
    "plain text",
}
FIGURE_REGION_LAYOUTS = {
    "figure",
    "figure_text",
    "figure_text_hybrid",
}
CAPTION_LAYOUTS = {
    "caption",
    "caption_hybrid",
    "figure_caption",
    "figure caption",
    "table_caption",
    "formula_caption",
}
AUTHOR_LAYOUTS = {
    "author",
    "author_info_hybrid",
}
AFFILIATION_LAYOUTS = {
    "affiliation",
}
SECTION_HEADINGS = {
    "abstract",
    "introduction",
    "related work",
    "method",
    "methods",
    "experiments",
    "conclusion",
    "references",
}


@dataclass(frozen=True)
class HygieneBlock:
    text: str
    context: dict[str, Any] | None = None


def normalize_extracted_pdf_text(
    text: str,
    context: dict[str, Any] | None = None,
) -> str:
    has_geometry = bool((context or {}).get("chars"))
    text = _geometry_text(context) or str(text)
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = normalized.replace("\u00ad", "")
    normalized = normalized.replace("\u2010", "-").replace("\u2011", "-")
    normalized = normalized.replace("\u2012", "-").replace("\u2013", "-")
    normalized = normalized.replace("\u2014", "-")
    normalized = re.sub(
        CID_TEXT_RE,
        lambda match: CID_TEXT_REPLACEMENTS.get(match.group("code"), match.group(0)),
        normalized,
    )
    normalized = ARROW_PLACEHOLDER_RE.sub(" -> ", normalized)
    if not has_geometry:
        normalized = re.sub(r"(?<=\w)-\s+(?=\w)", "", normalized)
    normalized = _repair_glued_words(normalized)
    normalized = _repair_placeholder_boundaries(normalized)
    normalized = re.sub(r",(?=\S)", ", ", normalized)
    normalized = re.sub(r"(?<=[A-Za-z])(?=\d+\s*(?:[,;\u2020*]|\s*[A-Z]))", " ", normalized)
    normalized = re.sub(r"(?<=\d)(?=[A-Z][a-z])", " ", normalized)
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z](?:\b|[^a-z]))", " ", normalized)
    normalized = _repair_author_affiliation_boundaries(normalized, context)
    normalized = re.sub(r"\s+([,;])", r"\1", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"[ \t]*\n[ \t]*", "\n", normalized)
    return normalized.strip()


def normalize_text_blocks(blocks: list[HygieneBlock]) -> list[HygieneBlock]:
    result: list[HygieneBlock] = []
    for block in blocks:
        text = normalize_extracted_pdf_text(block.text, block.context)
        if result and _is_hyphenated_continuation(result[-1].text, text):
            previous = result.pop()
            result.append(
                HygieneBlock(
                    text=previous.text[:-1] + text.lstrip(),
                    context=_merge_contexts(previous.context, block.context),
                )
            )
            continue
        result.append(HygieneBlock(text=text, context=block.context))
    return result


def placeholder_sequence(text: str) -> list[str]:
    return [match.group(0) for match in PLACEHOLDER_RE.finditer(text)]


def paragraph_hygiene_context(paragraph, page=None) -> dict[str, Any]:
    context: dict[str, Any] = {
        "layout_label": getattr(paragraph, "layout_label", None),
        "debug_id": getattr(paragraph, "debug_id", None),
        "bbox": _box_to_list(getattr(paragraph, "box", None)),
        "lines": [],
        "chars": [],
    }
    if page is not None:
        context["page_number"] = getattr(page, "page_number", None)
        context["page_layouts"] = [
            {
                "class_name": getattr(layout, "class_name", None),
                "bbox": _box_to_list(getattr(layout, "box", None)),
            }
            for layout in getattr(page, "page_layout", []) or []
        ]
    for composition in getattr(paragraph, "pdf_paragraph_composition", []) or []:
        line = getattr(composition, "pdf_line", None)
        chars = _composition_chars(composition)
        context["chars"].extend(_char_contexts(chars))
        if line is not None:
            context["lines"].append(
                {
                    "text": "".join(str(getattr(char, "char_unicode", "") or "") for char in chars),
                    "bbox": _box_to_list(getattr(line, "box", None)),
                    "spans": _line_spans(chars),
                }
            )
    return context


def is_figure_label_candidate(
    text: str,
    layout_label: str | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    return classify_text_role(text, layout_label, context) == "figure_label"


def classify_text_role(
    text: str,
    layout_label: str | None = None,
    context: dict[str, Any] | None = None,
) -> str | None:
    label = (layout_label or (context or {}).get("layout_label") or "").lower()
    cleaned = normalize_extracted_pdf_text(text, context)
    compact = cleaned.strip()
    bbox = (context or {}).get("bbox")
    if label in AUTHOR_LAYOUTS:
        return "author"
    if label in AFFILIATION_LAYOUTS:
        return "affiliation"
    if label in CAPTION_LAYOUTS or _bbox_overlaps_layout(bbox, context, CAPTION_LAYOUTS):
        return "caption"
    if not compact or compact.lower() in SECTION_HEADINGS:
        return None
    if len(compact) > 32:
        return None
    if re.search(r"[.!?;:]", compact):
        return None
    words = re.findall(r"[A-Za-z][A-Za-z-]*", compact)
    if not words or len(words) > 3:
        return None
    if label in FIGURE_LABEL_LAYOUTS:
        return "figure_label"
    if label == "fallback_line" and _bbox_overlaps_layout(
        bbox,
        context,
        FIGURE_REGION_LAYOUTS,
    ):
        return "figure_label"
    return None


def _repair_glued_words(text: str) -> str:
    result = text
    for glued, replacement in GLUED_WORD_REPAIRS:
        result = re.sub(glued, replacement, result, flags=re.IGNORECASE)
    return result


def _repair_placeholder_boundaries(text: str) -> str:
    text = re.sub(r"(?<=[A-Za-z0-9])(<b\d+>)(?=[A-Z0-9])", r" \1", text)
    text = re.sub(r"(</b\d+>)(?=[A-Z0-9])", r"\1 ", text)
    text = re.sub(r"(?<=[A-Za-z])(<b\d+></b\d+>)(?=[A-Z])", r" \1", text)
    return text


def _geometry_text(context: dict[str, Any] | None) -> str | None:
    chars = list((context or {}).get("chars") or [])
    if not chars:
        return None
    space_widths = [
        _bbox_width(char.get("bbox"))
        for char in chars
        if str(char.get("text") or "").isspace()
    ]
    space_widths = [width for width in space_widths if width > 0]
    if space_widths:
        gap_threshold = max(1.0, statistics.median(space_widths) * 0.75)
    else:
        gaps = [
            _char_gap(left, right)
            for left, right in zip(chars, chars[1:])
            if _same_text_line(left, right)
        ]
        positive_gaps = [gap for gap in gaps if gap > 0.5]
        gap_threshold = (
            max(1.5, statistics.median(positive_gaps) * 1.5)
            if positive_gaps
            else 1.5
        )

    rebuilt: list[str] = []
    previous: dict[str, Any] | None = None
    for char in chars:
        value = str(char.get("text") or "")
        if previous is not None:
            if not _same_text_line(previous, char):
                if str(previous.get("text") or "") == "-" and value[:1].islower():
                    if not _preserve_line_break_hyphen(rebuilt):
                        rebuilt.pop()
                else:
                    _append_space(rebuilt)
            elif (
                _char_gap(previous, char) >= gap_threshold
                and str(previous.get("text") or "") != "-"
                and not value.isspace()
                and (not rebuilt or not rebuilt[-1].isspace())
            ):
                rebuilt.append(" ")
        rebuilt.append(value)
        previous = char
    return "".join(rebuilt)


def _append_space(parts: list[str]) -> None:
    if parts and not parts[-1].isspace():
        parts.append(" ")


def _preserve_line_break_hyphen(parts: list[str]) -> bool:
    tail = "".join(parts[-16:]).lower()
    return tail.endswith(("text-to-", "text-", "real-"))


def _same_text_line(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_box = left.get("bbox")
    right_box = right.get("bbox")
    if not left_box or not right_box:
        return True
    if any(value is None for value in left_box[:4] + right_box[:4]):
        return True
    left_mid = (float(left_box[1]) + float(left_box[3])) / 2
    right_mid = (float(right_box[1]) + float(right_box[3])) / 2
    height = max(float(left_box[3]) - float(left_box[1]), 1.0)
    return abs(left_mid - right_mid) <= height * 0.8


def _char_gap(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_box = left.get("bbox")
    right_box = right.get("bbox")
    if not left_box or not right_box:
        return 0.0
    if any(value is None for value in left_box[:4] + right_box[:4]):
        return 0.0
    return float(right_box[0]) - float(left_box[2])


def _bbox_width(bbox: list[float | None] | None) -> float:
    if not bbox or len(bbox) < 4 or bbox[0] is None or bbox[2] is None:
        return 0.0
    return max(0.0, float(bbox[2]) - float(bbox[0]))


def _repair_author_affiliation_boundaries(
    text: str,
    context: dict[str, Any] | None,
) -> str:
    layout_label = ((context or {}).get("layout_label") or "").lower()
    if layout_label in {"author", "affiliation"} or _looks_like_author_affiliation(text):
        text = re.sub(r"(?<=[A-Za-z])(?=\d+\s*[A-Z])", " ", text)
        text = re.sub(r"(?<=\d)(?=[A-Z][a-z])", " ", text)
        text = re.sub(r"(?<=[a-z])(?=\d[A-Z])", " ", text)
    return text


def _looks_like_author_affiliation(text: str) -> bool:
    return bool(
        re.search(r"\bUniversity\b|\bInstitute\b|\bCollege\b|\bLaboratory\b", text)
        or re.search(r"[A-Z][a-z]+\s+[A-Z][a-z]+ ?\d", text)
    )


def _bbox_overlaps_layout(
    bbox: list[float | None] | None,
    context: dict[str, Any] | None,
    layout_names: set[str],
) -> bool:
    if not bbox:
        return False
    for layout in (context or {}).get("page_layouts", []) or []:
        class_name = str(layout.get("class_name") or "").lower()
        if class_name not in layout_names:
            continue
        layout_bbox = layout.get("bbox")
        if _bbox_overlap_ratio(bbox, layout_bbox) > 0.6:
            return True
    return False


def _bbox_overlap_ratio(
    a: list[float | None] | None,
    b: list[float | None] | None,
) -> float:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    if any(value is None for value in a[:4] + b[:4]):
        return 0.0
    ax1, ay1, ax2, ay2 = (float(value) for value in a)
    bx1, by1, bx2, by2 = (float(value) for value in b)
    width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    height = max(0.0, min(ay2, by2) - max(ay1, by1))
    area = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    if area == 0:
        return 0.0
    return (width * height) / area


def _is_hyphenated_continuation(left: str, right: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}-$", left.strip()) and re.match(r"^\s*[a-z]{2,}", right))


def _merge_contexts(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not left and not right:
        return None
    merged = dict(left or {})
    if right:
        merged.setdefault("merged_from", [])
        merged["merged_from"].append(
            {
                "debug_id": right.get("debug_id"),
                "layout_label": right.get("layout_label"),
                "bbox": right.get("bbox"),
            }
        )
        merged["lines"] = list(merged.get("lines") or []) + list(right.get("lines") or [])
    return merged


def _line_spans(chars: list[Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for char in chars:
        style = getattr(char, "pdf_style", None)
        key = (
            getattr(style, "font_id", None),
            getattr(style, "font_size", None),
        )
        value = str(getattr(char, "char_unicode", "") or "")
        if current is None or current["font"] != key[0] or current["font_size"] != key[1]:
            current = {
                "text": value,
                "font": key[0],
                "font_size": key[1],
                "bbox": _box_to_list(getattr(char, "visual_bbox", None)),
            }
            spans.append(current)
        else:
            current["text"] += value
    return spans


def _composition_chars(composition: Any) -> list[Any]:
    line = getattr(composition, "pdf_line", None)
    if line is not None:
        return list(getattr(line, "pdf_character", []) or [])
    same_style = getattr(composition, "pdf_same_style_characters", None)
    if same_style is not None:
        return list(getattr(same_style, "pdf_character", []) or [])
    formula = getattr(composition, "pdf_formula", None)
    if formula is not None:
        return list(getattr(formula, "pdf_character", []) or [])
    char = getattr(composition, "pdf_character", None)
    return [char] if char is not None else []


def _char_contexts(chars: list[Any]) -> list[dict[str, Any]]:
    result = []
    for char in chars:
        style = getattr(char, "pdf_style", None)
        result.append(
            {
                "text": str(getattr(char, "char_unicode", "") or ""),
                "font": getattr(style, "font_id", None),
                "font_size": getattr(style, "font_size", None),
                "bbox": _box_to_list(getattr(char, "visual_bbox", None)),
            }
        )
    return result


def _box_to_list(box_like) -> list[float | None] | None:
    box = getattr(box_like, "box", box_like)
    if box is None:
        return None
    return [
        getattr(box, "x", None),
        getattr(box, "y", None),
        getattr(box, "x2", None),
        getattr(box, "y2", None),
    ]
