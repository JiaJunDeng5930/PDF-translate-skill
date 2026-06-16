# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from .text_hygiene import normalize_extracted_pdf_text
from .text_hygiene import placeholder_sequence


@dataclass
class EditableBlock:
    source: str
    translation: str = ""
    context_before: str | None = None
    text_role: str | None = None


@dataclass
class EditableDocument:
    task: str
    items: list[EditableBlock]


class _EditableYamlDumper(yaml.SafeDumper):
    pass


def _represent_string(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_EditableYamlDumper.add_representer(str, _represent_string)


def render_editable_document(
    task_type: str,
    blocks: list[EditableBlock],
    target_language: str | None = None,
) -> str:
    data: dict[str, Any] = {"task": task_type}
    if target_language:
        data["target_language"] = target_language
    data["items"] = [
        _editable_item_for_dump(task_type, index, block)
        for index, block in enumerate(blocks, start=1)
    ]
    return (
        yaml.dump(
            data,
            Dumper=_EditableYamlDumper,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        ).strip()
        + "\n"
    )


def _editable_item_for_dump(
    task_type: str,
    index: int,
    block: EditableBlock,
) -> dict[str, Any]:
    item: dict[str, Any] = {"id": index, "source": block.source}
    if block.context_before:
        item["context_before"] = block.context_before
    if block.text_role:
        item["text_role"] = block.text_role
    item["translation"] = block.translation
    return item


def parse_editable_document(text: str) -> tuple[EditableDocument | None, list[str]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"YAML parse error: {exc}"]

    if not isinstance(data, dict):
        return None, ["editable file must be a YAML mapping"]

    task = data.get("task")
    if task != "translate":
        return None, ["task must be translate"]

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None, ["items must be a YAML list"]

    items: list[EditableBlock] = []
    errors: list[str] = []
    for index, raw_item in enumerate(raw_items, start=1):
        item, item_errors = _parse_item(task, raw_item, index)
        errors.extend(item_errors)
        if item is not None:
            items.append(item)
    if errors:
        return None, errors
    return EditableDocument(task=task, items=items), []


def _parse_item(
    task: str,
    raw_item,
    index: int,
) -> tuple[EditableBlock | None, list[str]]:
    if not isinstance(raw_item, dict):
        return None, [f"item {index}: expected a YAML mapping"]

    source = raw_item.get("source")
    if not isinstance(source, str):
        return None, [f"item {index}: source must be a string"]

    translation = raw_item.get("translation", "")
    if translation is None:
        translation = ""
    if not isinstance(translation, str):
        return None, [f"item {index}: translation must be a string"]
    return EditableBlock(source=source, translation=translation), []
