# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .editable import EditableBlock
from .editable import internal_placeholder_markup_tokens
from .editable import marker_sequence
from .editable import parse_editable_document
from .editable import render_editable_document
from .editable import restore_internal_placeholders
from .editable import strip_internal_placeholder_markup
from .editable import token_map_marker_sequence
from .editable import unknown_marker_sequence
from .state import WorkspacePaths
from .state import append_trace
from .state import archive_current_translation
from .state import atomic_write_text
from .state import save_accepted_answer
from .state import write_json


@dataclass
class ValidationResult:
    accepted: bool
    status: str
    errors: list[str]
    warnings: list[str]


def validate_pending(paths: WorkspacePaths, state: dict) -> ValidationResult:
    pending = state.get("pending")
    if not pending:
        return ValidationResult(False, "no_pending", [], [])

    if not paths.current_translation.exists():
        _restore_from_snapshot(paths, pending)
        return ValidationResult(
            False,
            "needs_ai_edit",
            [f"editable file was missing and has been restored: {paths.current_translation}"],
            [],
        )

    text = paths.current_translation.read_text(encoding="utf-8")
    document, parse_errors = parse_editable_document(text)
    if parse_errors:
        archive_current_translation(paths, pending["task_hash"], accepted=False)
        _restore_from_snapshot(paths, pending)
        state["status"] = "needs_ai_fix"
        write_json(paths.state, state)
        append_trace(
            paths,
            "answer_rejected",
            task_hash=pending["task_hash"],
            reason="block_structure",
            errors=parse_errors,
        )
        return ValidationResult(False, "needs_ai_fix", parse_errors, [])

    assert document is not None
    blocks = document.items
    if document.task != pending["task_type"]:
        errors = [
            f"editable task is {document.task}, expected {pending['task_type']}",
        ]
        archive_current_translation(paths, pending["task_hash"], accepted=False)
        _restore_from_snapshot(paths, pending)
        state["status"] = "needs_ai_fix"
        write_json(paths.state, state)
        append_trace(
            paths,
            "answer_rejected",
            task_hash=pending["task_hash"],
            reason="task_mismatch",
            errors=errors,
        )
        return ValidationResult(False, "needs_ai_fix", errors, [])

    errors = _validate_sources(pending, blocks)
    errors.extend(_validate_snapshot_markers(pending))
    if errors:
        append_trace(
            paths,
            "answer_rejected",
            task_hash=pending["task_hash"],
            reason="source_mismatch",
            errors=errors,
        )
        state["status"] = "needs_ai_fix"
        write_json(paths.state, state)
        return ValidationResult(False, "needs_ai_fix", errors, [])

    if pending["task_type"] == "translate":
        answer, errors, warnings = _build_translation_answer(pending, blocks)
    elif pending["task_type"] == "term_extract":
        answer, errors, warnings = _build_term_answer(pending, blocks)
    else:
        errors = [f"unknown task type: {pending['task_type']}"]
        answer = None
        warnings = []

    if errors:
        append_trace(
            paths,
            "answer_rejected",
            task_hash=pending["task_hash"],
            reason="validation",
            errors=errors,
        )
        state["status"] = "needs_ai_fix"
        write_json(paths.state, state)
        return ValidationResult(False, "needs_ai_fix", errors, warnings)

    archive_current_translation(paths, pending["task_hash"], accepted=True)
    summary = _build_answer_summary(pending, blocks, answer, warnings)
    save_accepted_answer(paths, state, pending["task_hash"], answer, summary)
    return ValidationResult(True, "accepted", [], warnings)


def _restore_from_snapshot(paths: WorkspacePaths, pending: dict) -> None:
    blocks = [
        EditableBlock(source=block["source"], translation="")
        for block in pending["blocks"]
    ]
    atomic_write_text(
        paths.current_translation,
        render_editable_document(
            pending["task_type"],
            blocks,
            pending.get("lang_out"),
        ),
    )


def _validate_sources(pending: dict, blocks: list[EditableBlock]) -> list[str]:
    errors = []
    expected = pending["blocks"]
    if len(blocks) != len(expected):
        errors.append(
            f"expected {len(expected)} source blocks, found {len(blocks)} blocks"
        )
        return errors
    for index, (block, snapshot) in enumerate(zip(blocks, expected), start=1):
        if block.source != snapshot["source"]:
            errors.append(f"source block {index} was modified")
    return errors


def _validate_snapshot_markers(pending: dict) -> list[str]:
    errors = []
    for index, snapshot in enumerate(pending["blocks"], start=1):
        token_map = snapshot.get("token_map", [])
        known_markers = token_map_marker_sequence(token_map)
        required_markers = snapshot.get("required_markers", [])
        unknown_required = [
            marker for marker in required_markers if marker not in known_markers
        ]
        for marker in unknown_required:
            errors.append(
                f"source block {index} requires unknown protected marker {marker}"
            )

        duplicate_required = sorted(
            {
                marker
                for marker in required_markers
                if required_markers.count(marker) > 1
            }
        )
        for marker in duplicate_required:
            errors.append(
                f"source block {index} repeats impossible protected marker {marker}"
            )

        source_markers = marker_sequence(snapshot["source"], token_map)
        if source_markers != required_markers:
            errors.append(
                f"source block {index} protected marker snapshot is inconsistent"
            )
        if source_markers != known_markers:
            errors.append(
                f"source block {index} protected marker map is inconsistent"
            )
    return errors


def _build_answer_summary(
    pending: dict,
    blocks: list[EditableBlock],
    answer,
    warnings: list[str],
) -> dict:
    translations = [block.translation for block in blocks]
    summary = {
        "task_type": pending["task_type"],
        "block_count": len(blocks),
        "filled_translation_blocks": sum(1 for text in translations if text.strip()),
        "translation_characters": sum(len(text) for text in translations),
        "protected_marker_count": sum(
            len(marker_sequence(text)) for text in translations
        ),
        "warning_count": len(warnings),
    }
    if pending["task_type"] == "term_extract":
        summary["term_pair_count"] = len(answer or [])
    return summary


def _build_translation_answer(
    pending: dict,
    blocks: list[EditableBlock],
) -> tuple[list[dict], list[str], list[str]]:
    errors = []
    warnings = []
    answer = []
    for index, (block, snapshot) in enumerate(
        zip(blocks, pending["blocks"]),
        start=1,
    ):
        if not block.translation.strip():
            errors.append(f"translation block {index} is empty")
            continue
        raw_internal_tags = internal_placeholder_markup_tokens(block.translation)
        if raw_internal_tags:
            errors.append(
                f"translation block {index} contains raw internal tag "
                f"{raw_internal_tags[0]}; use protected markers from the source"
            )
            continue
        expected_markers = snapshot["required_markers"]
        token_map = snapshot.get("token_map", [])
        unknown_markers = unknown_marker_sequence(block.translation, token_map)
        if unknown_markers:
            errors.append(
                f"translation block {index} contains unknown protected marker "
                + ", ".join(unknown_markers)
            )
            continue
        actual_markers = marker_sequence(block.translation, token_map)
        if actual_markers != expected_markers:
            errors.append(
                f"translation block {index} protected marker sequence mismatch"
            )
            continue
        if block.translation.strip() == block.source.strip():
            warnings.append(f"translation block {index} is identical to source")
        restored = restore_internal_placeholders(
            block.translation,
            token_map,
        )
        output = strip_internal_placeholder_markup(restored)
        answer.append({"id": index - 1, "output": output})
    return answer, errors, warnings


def _build_term_answer(
    pending: dict,
    blocks: list[EditableBlock],
) -> tuple[list[dict], list[str], list[str]]:
    errors = []
    warnings = []
    answer = []
    seen_sources: dict[str, str] = {}

    for index, (block, snapshot) in enumerate(
        zip(blocks, pending["blocks"]),
        start=1,
    ):
        source_text = snapshot["source"]
        original_source = snapshot.get("original_source", source_text)
        for pair in block.terms:
            source = pair.source
            target = pair.target
            if len(source) >= 100:
                errors.append(f"block {index}: source term is too long: {source[:80]}")
                continue
            if not _source_contains_term(source_text, original_source, source):
                errors.append(
                    f"block {index}: source term does not occur in the source text: {source}"
                )
                continue
            source_key = _term_conflict_key(source)
            if source_key in seen_sources and seen_sources[source_key] != target:
                warnings.append(f"term has conflicting translations: {source}")
            seen_sources[source_key] = target
            answer.append({"src": source, "tgt": target})

    return answer, errors, warnings


def _source_contains_term(source_text: str, original_source: str, term: str) -> bool:
    term_variants = _pdf_text_variants(term)
    if not term_variants:
        return False
    source_variants = _pdf_text_variants(source_text) | _pdf_text_variants(
        original_source
    )
    return any(
        term_variant in source_variant
        for source_variant in source_variants
        for term_variant in term_variants
    )


def _pdf_text_variants(text: str) -> set[str]:
    base = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    base = base.lower()
    base = base.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-")
    base = base.replace("\u2013", "-").replace("\u2014", "-")
    base = re.sub(r"\s+", " ", base).strip()
    if not base:
        return set()

    hyphen_tight = re.sub(r"\s*-\s*", "-", base)
    dehyphenated_breaks = re.sub(r"(?<=\w)-\s+(?=\w)", "", base)
    dehyphenated_tight = re.sub(r"(?<=\w)-(?=\w)", "", hyphen_tight)
    compact = re.sub(r"[\s\-]+", "", base)
    return {base, hyphen_tight, dehyphenated_breaks, dehyphenated_tight, compact}


def _term_conflict_key(term: str) -> str:
    variants = _pdf_text_variants(term)
    if not variants:
        return ""
    return min(variants, key=lambda item: (len(item), item))
