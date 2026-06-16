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
from .text_hygiene import HygieneBlock
from .text_hygiene import normalize_text_blocks

TRANSLATION_INPUT_MARKER = "## Here is the input:"
SINGLE_TEXT_MARKER = "Now translate the following text:"
TERM_INPUT_RE = re.compile(
    r"Input Text:\s*```\n(?P<source>.*?)\n```\s*\n\s*Return JSON",
    re.DOTALL,
)


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

        term_match = TERM_INPUT_RE.search(prompt)
        if term_match:
            source = term_match.group("source")
            chunks = [chunk.strip() for chunk in source.split("\n\n") if chunk.strip()]
            return self._term_task_from_chunks(chunks)

        if SINGLE_TEXT_MARKER in prompt:
            source = prompt.split(SINGLE_TEXT_MARKER, 1)[1].strip()
            return self._translation_task_from_items([{"id": 0, "input": source}])

        raise RuntimeError("unsupported file-task LLM prompt shape")

    def _translation_task_from_items(self, items: list[dict]) -> dict:
        active_page = self._active_page()
        blocks = []
        for index, item in enumerate(items):
            original_source = item.get("input", "")
            context = item.get("hygiene_context")
            display_source = normalize_extracted_pdf_text(original_source, context)
            context_before = (
                self._previous_page_context(display_source) if index == 0 else None
            )
            blocks.append(
                {
                    "source": display_source,
                    "original_source": original_source,
                    "required_placeholders": placeholder_sequence(display_source),
                    "layout_label": item.get("layout_label"),
                    "text_role": item.get("text_role"),
                    "hygiene_context": context,
                    "context_before": context_before,
                }
            )
        _repair_translation_block_boundaries(blocks)

        hash_blocks = [_hashable_block(block) for block in blocks]
        hash_payload = {
            "task_type": "translate",
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "page": active_page,
            "blocks": hash_blocks,
        }
        task_hash = stable_hash(hash_payload)
        return {
            "task_type": "translate",
            "task_hash": task_hash,
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "page": active_page,
            "blocks": blocks,
        }

    def _term_task_from_chunks(self, chunks: list[str]) -> dict:
        active_page = self._active_page()
        blocks = []
        hygiene_blocks = normalize_text_blocks(
            [HygieneBlock(text=chunk) for chunk in chunks]
        )
        for index, block in enumerate(hygiene_blocks):
            display_source = block.text
            context_before = (
                self._previous_page_context(display_source) if index == 0 else None
            )
            blocks.append(
                {
                    "source": display_source,
                    "original_source": display_source,
                    "required_placeholders": placeholder_sequence(display_source),
                    "context_before": context_before,
                }
            )
        hash_payload = {
            "task_type": "term_extract",
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "page": active_page,
            "blocks": [_hashable_block(block) for block in blocks],
        }
        task_hash = stable_hash(hash_payload)
        return {
            "task_type": "term_extract",
            "task_hash": task_hash,
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "page": active_page,
            "blocks": blocks,
        }

    def _previous_page_context(self, source: str) -> str | None:
        active_page = self._active_page()
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
