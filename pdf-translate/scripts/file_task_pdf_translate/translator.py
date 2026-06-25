# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import json
import re

from babeldoc.file_task_bridge import FileTaskPending
from babeldoc.translator.translator import BaseTranslator

from .editable import EditableBlock
from .editable import normalize_extracted_pdf_text
from .editable import placeholder_sequence
from .state import WorkspacePaths
from .state import load_accepted_answer
from .state import save_pending_task
from .state import stable_hash

TRANSLATION_INPUT_MARKER = "## Here is the input:"
SINGLE_TEXT_MARKER = "Now translate the following text:"


class FileTaskTranslator(BaseTranslator):
    name = "file-task"

    def __init__(self, paths: WorkspacePaths, state: dict):
        config = state["config"]
        super().__init__(config["lang_in"], config["lang_out"])
        self.paths = paths
        self.state = state
        self.model = "current_translation"

    def _active_page(self) -> int | None:
        page_plan = self.state.get("page_plan") or {}
        active_page = page_plan.get("active_page")
        return active_page if isinstance(active_page, int) else None

    def _task_page_from_items(self, items: list[dict]) -> int | list[int] | None:
        active_page = self._active_page()
        if active_page is not None:
            return active_page

        pages: list[int] = []
        for item in items:
            page = item.get("page")
            if page is None:
                page = (item.get("hygiene_context") or {}).get("page_number")
                if isinstance(page, int):
                    page += 1
            if isinstance(page, int) and page not in pages:
                pages.append(page)
        if len(pages) == 1:
            return pages[0]
        return pages or None

    def do_llm_translate(self, text, rate_limit_params: dict = None):
        if text is None:
            return ""
        task = self._task_from_prompt(str(text))
        answer = load_accepted_answer(self.paths, self.state, task["task_hash"])
        if answer is not None:
            return json.dumps(answer, ensure_ascii=False)

        blocks = [_editable_block(block) for block in task["blocks"]]
        save_pending_task(self.paths, self.state, task, blocks)
        raise FileTaskPending(task["task_hash"])

    def do_translate(self, text, rate_limit_params: dict = None):
        task = self._translation_task_from_items([{"id": 0, "input": str(text)}])
        answer = load_accepted_answer(self.paths, self.state, task["task_hash"])
        if answer is not None:
            if isinstance(answer, list) and answer:
                return answer[0].get("output", text)
            return text
        blocks = [_editable_block(block) for block in task["blocks"]]
        save_pending_task(self.paths, self.state, task, blocks)
        raise FileTaskPending(task["task_hash"])

    def _task_from_prompt(self, prompt: str) -> dict:
        if TRANSLATION_INPUT_MARKER in prompt:
            json_input = prompt.split(TRANSLATION_INPUT_MARKER, 1)[1].strip()
            items = json.loads(json_input)
            return self._translation_task_from_items(items)

        if SINGLE_TEXT_MARKER in prompt:
            source = prompt.split(SINGLE_TEXT_MARKER, 1)[1].strip()
            return self._translation_task_from_items([{"id": 0, "input": source}])

        raise RuntimeError("unsupported file-task LLM prompt shape")

    def _translation_task_from_items(self, items: list[dict]) -> dict:
        task_page = self._task_page_from_items(items)
        context_page = task_page if isinstance(task_page, int) else None
        blocks = []
        for index, item in enumerate(items):
            original_source = item.get("input", "")
            context = item.get("hygiene_context")
            display_source = normalize_extracted_pdf_text(original_source, context)
            context_before = (
                self._previous_page_context(display_source, context_page)
                if index == 0
                else None
            )
            block = {
                "id": item.get("id", index),
                "source": display_source,
                "original_source": original_source,
                "required_placeholders": placeholder_sequence(display_source),
                "layout_label": item.get("layout_label"),
                "text_role": item.get("text_role"),
                "hygiene_context": context,
                "context_before": context_before,
            }
            page = item.get("page")
            if isinstance(page, int):
                block["page"] = page
            blocks.append(block)
        _repair_translation_block_boundaries(blocks)
        for block in blocks:
            block["source_hash"] = stable_hash(
                {
                    "source": block["source"],
                    "original_source": block["original_source"],
                }
            )

        hash_blocks = [_hashable_block(block) for block in blocks]
        hash_payload = {
            "task_type": "translate",
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "page": task_page,
            "blocks": hash_blocks,
        }
        task_hash = stable_hash(hash_payload)
        return {
            "task_type": "translate",
            "task_hash": task_hash,
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "page": task_page,
            "blocks": blocks,
        }

    def _previous_page_context(self, source: str, active_page: int | None) -> str | None:
        if not active_page or active_page <= 1:
            return None
        stripped = source.lstrip()
        if not stripped or not stripped[:1].islower():
            return None
        try:
            import pymupdf

            document = pymupdf.open(self.state["config"]["input_pdf"])
            try:
                previous_text = document[active_page - 2].get_text("text")
            finally:
                document.close()
        except Exception:
            return None
        words = " ".join(previous_text.split())
        return words[-360:] if words else None


def _hashable_block(block: dict) -> dict:
    return {
        key: value
        for key, value in block.items()
        if key != "hygiene_context"
    }


def _editable_block(block: dict) -> EditableBlock:
    return EditableBlock(
        source=block["source"],
        translation="",
        context_before=block.get("context_before"),
        text_role=block.get("text_role"),
    )


def _repair_translation_block_boundaries(blocks: list[dict]) -> None:
    for index in range(len(blocks) - 1):
        current = blocks[index]
        following = blocks[index + 1]
        left = current.get("source") or ""
        right = following.get("source") or ""
        match = re.match(r"^([a-z]{2,})(\s+|$)", right)
        if not match or not re.search(r"[A-Za-z]{2,}-$", left.rstrip()):
            continue
        word = match.group(1)
        current["source"] = re.sub(r"-\s*$", "", left.rstrip()) + word
        following["source"] = right[match.end() :].lstrip()
        current["required_placeholders"] = placeholder_sequence(current["source"])
        following["required_placeholders"] = placeholder_sequence(following["source"])
