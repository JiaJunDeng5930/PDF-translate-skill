# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import json
import re
from pathlib import Path

from babeldoc.file_task_bridge import FileTaskPending
from babeldoc.translator.translator import BaseTranslator

from .editable import EditableBlock
from .editable import marker_sequence
from .editable import normalize_extracted_pdf_text
from .editable import replace_internal_placeholders
from .state import WorkspacePaths
from .state import load_accepted_answer
from .state import save_pending_task
from .state import stable_hash

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
        super().__init__(config["lang_in"], config["lang_out"], ignore_cache=True)
        self.paths = paths
        self.state = state
        self.model = "current_translation"

    def do_llm_translate(self, text, rate_limit_params: dict = None):
        if text is None:
            return ""
        task = self._task_from_prompt(str(text))
        answer = load_accepted_answer(self.paths, self.state, task["task_hash"])
        if answer is not None:
            return json.dumps(answer, ensure_ascii=False)

        blocks = [
            EditableBlock(source=block["source"], translation="")
            for block in task["blocks"]
        ]
        save_pending_task(self.paths, self.state, task, blocks)
        raise FileTaskPending(task["task_hash"])

    def do_translate(self, text, rate_limit_params: dict = None):
        task = self._translation_task_from_items([{"id": 0, "input": str(text)}])
        answer = load_accepted_answer(self.paths, self.state, task["task_hash"])
        if answer is not None:
            if isinstance(answer, list) and answer:
                return answer[0].get("output", text)
            return text
        blocks = [
            EditableBlock(source=block["source"], translation="")
            for block in task["blocks"]
        ]
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
        blocks = []
        for item in items:
            original_source = item.get("input", "")
            display_input = normalize_extracted_pdf_text(original_source)
            formula_tokens = set((item.get("formula_placeholders_hint") or {}).keys())
            display_source, token_map = replace_internal_placeholders(
                display_input,
                formula_tokens,
            )
            blocks.append(
                {
                    "source": display_source,
                    "original_source": original_source,
                    "token_map": token_map,
                    "required_markers": marker_sequence(display_source, token_map),
                    "layout_label": item.get("layout_label"),
                }
            )

        hash_payload = {
            "task_type": "translate",
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "blocks": blocks,
        }
        task_hash = stable_hash(hash_payload)
        return {
            "task_type": "translate",
            "task_hash": task_hash,
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "blocks": blocks,
        }

    def _term_task_from_chunks(self, chunks: list[str]) -> dict:
        blocks = []
        for chunk in chunks:
            display_input = normalize_extracted_pdf_text(chunk)
            display_source, token_map = replace_internal_placeholders(display_input)
            blocks.append(
                {
                    "source": display_source,
                    "original_source": chunk,
                    "token_map": token_map,
                    "required_markers": marker_sequence(display_source, token_map),
                }
            )
        hash_payload = {
            "task_type": "term_extract",
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "blocks": blocks,
        }
        task_hash = stable_hash(hash_payload)
        return {
            "task_type": "term_extract",
            "task_hash": task_hash,
            "lang_in": self.state["config"]["lang_in"],
            "lang_out": self.state["config"]["lang_out"],
            "blocks": blocks,
        }
