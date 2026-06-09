# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

from dataclasses import dataclass

from .editable import EditableBlock
from .editable import marker_sequence
from .editable import parse_blocks
from .editable import parse_term_translation
from .editable import render_blocks
from .editable import restore_internal_placeholders
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
    blocks, parse_errors = parse_blocks(text)
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

    errors = _validate_sources(pending, blocks)
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
    atomic_write_text(paths.current_translation, render_blocks(blocks))


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
        expected_markers = snapshot["required_markers"]
        actual_markers = marker_sequence(block.translation)
        if actual_markers != expected_markers:
            errors.append(
                f"translation block {index} protected marker sequence mismatch"
            )
            continue
        if block.translation.strip() == block.source.strip():
            warnings.append(f"translation block {index} is identical to source")
        restored = restore_internal_placeholders(
            block.translation,
            snapshot.get("token_map", []),
        )
        answer.append({"id": index - 1, "output": restored})
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
        if not block.translation.strip():
            errors.append(f"term extraction block {index} is empty; write [] if none")
            continue
        pairs, parse_errors = parse_term_translation(block.translation)
        errors.extend(f"block {index}: {error}" for error in parse_errors)
        source_text = snapshot["source"]
        original_source = snapshot.get("original_source", source_text)
        for source, target in pairs:
            if len(source) >= 100:
                errors.append(f"block {index}: source term is too long: {source[:80]}")
                continue
            if source not in source_text and source not in original_source:
                errors.append(
                    f"block {index}: source term does not occur in the source text: {source}"
                )
                continue
            if source in seen_sources and seen_sources[source] != target:
                warnings.append(f"term has conflicting translations: {source}")
            seen_sources[source] = target
            answer.append({"src": source, "tgt": target})

    return answer, errors, warnings
