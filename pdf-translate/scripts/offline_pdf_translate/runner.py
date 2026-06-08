from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

from babeldoc.format.pdf.high_level import translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.offline_bridge import OfflineTranslationPending

from .state import append_trace
from .state import load_or_init_state
from .state import paths_for
from .state import read_json
from .state import trace_tail
from .state import workspace_lock
from .state import write_json
from .translator import OfflineFileTranslator
from .validation import ValidationResult
from .validation import validate_pending

logger = logging.getLogger(__name__)


def advance(workspace: Path | None = None) -> dict:
    root = (workspace or Path.cwd()).resolve()
    paths = paths_for(root)
    with workspace_lock(paths):
        state = load_or_init_state(paths)
        state["advance_count"] = int(state.get("advance_count", 0)) + 1
        write_json(paths.state, state)

        validation = validate_pending(paths, state)
        if validation.status in {"needs_ai_edit", "needs_ai_fix"}:
            state = read_json(paths.state, state)
            return _pending_response(paths, state, validation)

        translator = OfflineFileTranslator(paths, state)
        config = _build_translation_config(paths, state, translator)
        config.offline_file_workflow = True

        try:
            result = translate(config)
        except OfflineTranslationPending:
            state = read_json(paths.state, state)
            return _pending_response(
                paths,
                state,
                ValidationResult(False, state.get("status", "needs_ai_edit"), [], []),
            )
        except Exception as exc:
            state["status"] = "error"
            state["last_error"] = str(exc)
            write_json(paths.state, state)
            append_trace(
                paths,
                "advance_error",
                error=str(exc),
                traceback=traceback.format_exc(limit=8),
            )
            return {
                "status": "error",
                "editable_file": None,
                "instruction": "Fix the runtime error reported in validation_errors.",
                "progress": _progress(state),
                "validation_errors": [str(exc)],
                "validation_warnings": [],
                "trace_tail": trace_tail(paths),
                "output_pdf": None,
            }

        output_pdf = result.mono_pdf_path or result.no_watermark_mono_pdf_path
        if output_pdf is None:
            output_pdf = result.dual_pdf_path or result.no_watermark_dual_pdf_path
        state["status"] = "done"
        state["pending"] = None
        state["output_pdf"] = str(output_pdf) if output_pdf else None
        write_json(paths.state, state)
        append_trace(paths, "pdf_written", output_pdf=state["output_pdf"])
        return {
            "status": "done",
            "editable_file": None,
            "instruction": "The translated PDF is complete.",
            "progress": _progress(state),
            "validation_errors": [],
            "validation_warnings": validation.warnings,
            "trace_tail": trace_tail(paths),
            "output_pdf": state["output_pdf"],
        }


def _build_translation_config(
    paths,
    state: dict,
    translator: OfflineFileTranslator,
) -> TranslationConfig:
    return TranslationConfig(
        translator=translator,
        term_extraction_translator=translator,
        input_file=state["input_pdf"],
        lang_in="en",
        lang_out="zh-CN",
        doc_layout_model=None,
        output_dir=paths.output,
        working_dir=paths.working,
        debug=False,
        no_dual=True,
        no_mono=False,
        qps=1,
        use_rich_pbar=False,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        add_formula_placehold_hint=True,
        pool_max_workers=1,
        term_pool_max_workers=1,
        auto_extract_glossary=True,
        disable_same_text_fallback=True,
    )


def _pending_response(
    paths,
    state: dict,
    validation: ValidationResult,
) -> dict:
    pending = state.get("pending") or {}
    status = validation.status
    if status == "accepted":
        status = state.get("status", "needs_ai_edit")
    return {
        "status": status if status != "no_pending" else state.get("status"),
        "editable_file": str(paths.current_translation),
        "instruction": _instruction_for_pending(pending, validation),
        "progress": _progress(state),
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
        "trace_tail": trace_tail(paths),
        "output_pdf": state.get("output_pdf"),
    }


def _instruction_for_pending(pending: dict, validation: ValidationResult) -> str:
    task_type = pending.get("task_type")
    if task_type == "term_extract":
        body = (
            "Fill each TRANSLATION block with term pairs from the matching SOURCE "
            "block, one pair per line as `source term → Chinese term`. Write `[]` "
            "when a source block has no terms."
        )
    elif task_type == "translate":
        body = (
            "Fill every TRANSLATION block with Simplified Chinese for the matching "
            "SOURCE block. Keep SOURCE blocks unchanged. Keep every protected marker "
            "such as `⟦FORMULA⟧` and `⟦PROTECTED_TEXT⟧` in the same order."
        )
    else:
        body = "Fill the TRANSLATION blocks in current_translation.txt."
    if validation.errors:
        return body + " Resolve validation_errors and run advance again."
    return body + " Save the file and run advance again."


def _progress(state: dict) -> dict:
    return {
        "advance_count": state.get("advance_count", 0),
        "accepted_tasks": len(state.get("accepted", {})),
        "pending_task_type": (state.get("pending") or {}).get("task_type"),
        "pending_blocks": len((state.get("pending") or {}).get("blocks", [])),
        "input_pdf": state.get("input_pdf"),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stdout,
    )
    result = advance()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "error" else 1
