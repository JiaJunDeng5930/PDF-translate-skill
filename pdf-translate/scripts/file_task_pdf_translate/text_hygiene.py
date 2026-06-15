# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import re
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
)
FIGURE_LABEL_LAYOUTS = {
    "figure",
    "figure_caption",
    "figure caption",
    "caption",
    "plain text",
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
    normalized = re.sub(r"(?<=\w)-\s+(?=\w)", "", normalized)
    normalized = _repair_glued_words(normalized)
    normalized = _repair_placeholder_boundaries(normalized)
    normalized = re.sub(r",(?=\S)", ", ", normalized)
    normalized = re.sub(r"(?<=[A-Za-z])(?=\d+(?:[,;\u2020*]|\s*[A-Z]))", " ", normalized)
    normalized = re.sub(r"(?<=\d)(?=[A-Z][a-z])", " ", normalized)
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z](?:\b|[^a-z]))", " ", normalized)
    normalized = _repair_author_affiliation_boundaries(normalized, context)
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
    }
    if page is not None:
        context["page_number"] = getattr(page, "page_number", None)
    for composition in getattr(paragraph, "pdf_paragraph_composition", []) or []:
        line = getattr(composition, "pdf_line", None)
        if line is None:
            continue
        chars = list(getattr(line, "pdf_character", []) or [])
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
    label = (layout_label or (context or {}).get("layout_label") or "").lower()
    cleaned = normalize_extracted_pdf_text(text, context)
    compact = cleaned.strip()
    if not compact or compact.lower() in SECTION_HEADINGS:
        return False
    if len(compact) > 32:
        return False
    if re.search(r"[.!?;:]", compact):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]*", compact)
    if not words or len(words) > 3:
        return False
    return label in FIGURE_LABEL_LAYOUTS


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
