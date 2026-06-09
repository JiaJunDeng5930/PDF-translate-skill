# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import re
from dataclasses import dataclass

SOURCE_MARKER = "⟦SOURCE⟧"
TRANSLATION_MARKER = "⟦TRANSLATION⟧"
END_MARKER = "⟦END⟧"

PROTECTED_MARKER_RE = re.compile(r"⟦(?:FORMULA|INLINE_MATH|PROTECTED_TEXT)⟧")
INTERNAL_PLACEHOLDER_RE = re.compile(r"</?b\d+>", re.IGNORECASE)
TERM_SEPARATORS = ("→", "->", "=>", "\t", "：", ":")
QUESTION_MARK_SEPARATOR_RE = re.compile(r"\s+\?\s+")


@dataclass
class EditableBlock:
    source: str
    translation: str


def render_blocks(blocks: list[EditableBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        parts.append(SOURCE_MARKER)
        parts.append(block.source)
        parts.append(TRANSLATION_MARKER)
        parts.append(block.translation)
        parts.append(END_MARKER)
        parts.append("")
    return "\n".join(parts)


def parse_blocks(text: str) -> tuple[list[EditableBlock], list[str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[EditableBlock] = []
    errors: list[str] = []
    state = "need_source"
    source_lines: list[str] = []
    translation_lines: list[str] = []

    for line_number, line in enumerate(lines, start=1):
        marker = line.strip()
        if state == "need_source":
            if marker == "":
                continue
            if marker != SOURCE_MARKER:
                errors.append(f"line {line_number}: expected {SOURCE_MARKER}")
                state = "damaged"
                break
            source_lines = []
            translation_lines = []
            state = "source"
            continue

        if state == "source":
            if marker == TRANSLATION_MARKER:
                state = "translation"
                continue
            if marker in (SOURCE_MARKER, END_MARKER):
                errors.append(f"line {line_number}: unexpected marker in source block")
                state = "damaged"
                break
            source_lines.append(line)
            continue

        if state == "translation":
            if marker == END_MARKER:
                blocks.append(
                    EditableBlock(
                        source="\n".join(source_lines),
                        translation="\n".join(translation_lines).strip(),
                    )
                )
                state = "need_source"
                continue
            if marker in (SOURCE_MARKER, TRANSLATION_MARKER):
                errors.append(
                    f"line {line_number}: unexpected marker in translation block"
                )
                state = "damaged"
                break
            translation_lines.append(line)

    if state == "source":
        errors.append(f"file ended before {TRANSLATION_MARKER}")
    elif state == "translation":
        errors.append(f"file ended before {END_MARKER}")
    elif state == "damaged":
        pass

    return blocks, errors


def marker_sequence(text: str) -> list[str]:
    return PROTECTED_MARKER_RE.findall(text)


def replace_internal_placeholders(
    text: str,
    formula_tokens: set[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    formula_tokens = formula_tokens or set()
    token_map: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        marker = "⟦FORMULA⟧" if token in formula_tokens else "⟦PROTECTED_TEXT⟧"
        token_map.append({"marker": marker, "token": token})
        return marker

    return INTERNAL_PLACEHOLDER_RE.sub(replace, text), token_map


def restore_internal_placeholders(text: str, token_map: list[dict[str, str]]) -> str:
    index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal index
        if index >= len(token_map):
            return match.group(0)
        token = token_map[index]["token"]
        index += 1
        return token

    return PROTECTED_MARKER_RE.sub(replace, text)


def parse_term_translation(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    stripped = text.strip()
    if stripped in {"[]", "无", "None", "none"}:
        return [], []

    terms: list[tuple[str, str]] = []
    errors: list[str] = []
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line or line in {"[]", "无", "None", "none"}:
            continue
        split = _split_term_line(line)
        if split is None:
            errors.append(f"cannot parse term line: {line[:80]}")
            continue
        source, target = split
        if not source or not target:
            errors.append(f"term line has empty side: {line[:80]}")
            continue
        terms.append((source, target))
    return terms, errors


def _split_term_line(line: str) -> tuple[str, str] | None:
    for separator in TERM_SEPARATORS:
        if separator in line:
            source, target = line.split(separator, 1)
            return source.strip().strip("-* "), target.strip()
    match = QUESTION_MARK_SEPARATOR_RE.search(line)
    if match is None:
        return None
    source = line[: match.start()]
    target = line[match.end() :]
    return source.strip().strip("-* "), target.strip()
