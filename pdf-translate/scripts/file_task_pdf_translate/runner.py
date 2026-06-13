# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import gc
import json
import logging
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from babeldoc.assets.assets import AssetError
from babeldoc.assets.assets import set_runtime_asset_dir
from babeldoc.const import close_process_pool
from babeldoc.file_task_bridge import FileTaskPending
from babeldoc.format.pdf.high_level import translate
from babeldoc.format.pdf.translation_config import TranslationConfig

from .config import ConfigError
from .config import load_workspace_config
from .config import output_flags
from .config import watermark_mode
from .state import append_trace
from .state import accepted_answer_count
from .state import load_pending_task
from .state import load_or_init_state
from .state import paths_for
from .state import read_json
from .state import trace_tail
from .state import workspace_lock
from .state import write_json
from .translator import FileTaskTranslator
from .validation import ValidationResult
from .validation import validate_pending

logger = logging.getLogger(__name__)
OUTPUT_INTERNAL_MARKER_RE = re.compile(
    r"</?b\d+>|\{\{(?:FORMULA|PROTECTED)_[0-9]+\}\}",
    re.IGNORECASE,
)


def advance(workspace: Path | None = None) -> dict:
    root = (workspace or Path.cwd()).resolve()
    paths = paths_for(root)
    with workspace_lock(paths) as lock_error:
        if lock_error is not None:
            return _lock_error_response(paths, lock_error)

        try:
            workspace_config = load_workspace_config(root)
        except ConfigError as exc:
            return _config_error_response(paths, str(exc), None)

        existing_state = read_json(paths.state, None)
        if existing_state is None:
            try:
                set_runtime_asset_dir(workspace_config.snapshot["asset_dir"])
            except AssetError as exc:
                return _asset_error_response(paths, str(exc), None)

        state = load_or_init_state(paths, workspace_config.snapshot)
        drift_error = _config_drift_error(state, workspace_config.snapshot)
        if drift_error:
            return _config_error_response(paths, drift_error, state)
        try:
            asset_dir = set_runtime_asset_dir(state["config"]["asset_dir"])
        except AssetError as exc:
            return _asset_error_response(paths, str(exc), state)
        append_trace(paths, "assets_ready", asset_dir=str(asset_dir))

        state["advance_count"] = int(state.get("advance_count", 0)) + 1
        write_json(paths.state, state)

        validation = validate_pending(paths, state)
        if validation.status in {"needs_ai_edit", "needs_ai_fix"}:
            state = read_json(paths.state, state)
            return _pending_response(paths, state, validation)

        translator = FileTaskTranslator(paths, state)
        config = _build_translation_config(paths, state, translator)
        config.file_task_workflow = True
        config.file_task_preprocess_cache_key = state["config"]["config_hash"]
        config.progress_change_callback = (
            lambda **event: _record_pipeline_progress(paths, event)
        )

        try:
            result = translate(config)
        except FileTaskPending:
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
                "progress": _progress(paths, state),
                "validation_errors": [str(exc)],
                "validation_warnings": [],
                "trace_tail": trace_tail(paths),
                "output_pdf": None,
                "output_pdfs": {},
            }
        finally:
            shutdown_file_task_runtime()

        output_pdfs, output_pdf = _collect_output_pdfs(result, state["config"])
        output_errors = _validate_output_pdfs(output_pdfs)
        if output_errors:
            state["status"] = "error"
            state["last_error"] = "; ".join(output_errors)
            state["pending_task_hash"] = None
            state["output_pdfs"] = output_pdfs
            write_json(paths.state, state)
            append_trace(paths, "output_validation_error", errors=output_errors)
            return {
                "status": "error",
                "editable_file": None,
                "instruction": "Fix the output validation errors reported in validation_errors.",
                "progress": _progress(paths, state),
                "validation_errors": output_errors,
                "validation_warnings": validation.warnings,
                "trace_tail": trace_tail(paths),
                "output_pdf": output_pdf,
                "output_pdfs": output_pdfs,
            }
        state["status"] = "done"
        state["pending_task_hash"] = None
        state["output_pdfs"] = output_pdfs
        state.pop("last_error", None)
        write_json(paths.state, state)
        _write_progress_snapshot(paths, _terminal_pipeline_progress())
        append_trace(paths, "pdf_written", output_pdf=output_pdf, output_pdfs=output_pdfs)
        return {
            "status": "done",
            "editable_file": None,
            "instruction": "The translated PDF is complete.",
            "progress": _progress(paths, state),
            "validation_errors": [],
            "validation_warnings": validation.warnings,
            "trace_tail": trace_tail(paths),
            "output_pdf": output_pdf,
            "output_pdfs": output_pdfs,
        }


def _build_translation_config(
    paths,
    state: dict,
    translator: FileTaskTranslator,
) -> TranslationConfig:
    config = state["config"]
    no_dual, no_mono = output_flags(config["output_mode"])
    return TranslationConfig(
        translator=translator,
        term_extraction_translator=translator,
        input_file=config["input_pdf"],
        lang_in=config["lang_in"],
        lang_out=config["lang_out"],
        doc_layout_model=None,
        pages=config["pages"],
        output_dir=paths.output,
        working_dir=paths.working,
        debug=False,
        no_dual=no_dual,
        no_mono=no_mono,
        use_rich_pbar=False,
        watermark_output_mode=watermark_mode(config["watermark_output_mode"]),
        add_formula_placehold_hint=config["add_formula_placehold_hint"],
        auto_extract_glossary=config["auto_extract_glossary"],
        primary_font_family=config["primary_font_family"],
        report_interval=2.0,
    )


def _pending_response(
    paths,
    state: dict,
    validation: ValidationResult,
) -> dict:
    pending = load_pending_task(paths, state) or {}
    status = validation.status
    if status == "accepted":
        status = state.get("status", "needs_ai_edit")
    return {
        "status": status if status != "no_pending" else state.get("status"),
        "editable_file": str(paths.current_translation),
        "instruction": _instruction_for_pending(pending, validation, state),
        "progress": _progress(paths, state),
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
        "trace_tail": trace_tail(paths),
        "output_pdf": _primary_output_pdf(state),
        "output_pdfs": state.get("output_pdfs", {}),
    }


def _instruction_for_pending(
    pending: dict,
    validation: ValidationResult,
    state: dict,
) -> str:
    task_type = pending.get("task_type")
    target_language = pending.get("lang_out") or state["config"]["lang_out"]
    if task_type == "term_extract":
        body = (
            f"Edit current_translation.yaml. For each item, write terms as YAML "
            f"list entries with source and target fields. Use {target_language} "
            "for target. Keep terms as [] when an item has no terms."
        )
    elif task_type == "translate":
        body = (
            f"Edit current_translation.yaml. Fill each item's translation field "
            f"with {target_language} text. Keep source fields unchanged. Preserve "
            "every placeholder such as <b1> and </b1> exactly, in the same order."
        )
    else:
        body = "Edit current_translation.yaml."
    if validation.errors:
        return body + " Resolve validation_errors and run advance again."
    return body + " Save the file and run advance again."


def _progress(paths, state: dict) -> dict:
    config = state.get("config") or {}
    pending = load_pending_task(paths, state) or {}
    pipeline_progress = read_json(paths.progress, None)
    if state.get("status") == "done":
        pipeline_progress = _terminal_pipeline_progress()
    elif pipeline_progress:
        pipeline_progress["paused_for_ai"] = state.get("status") in {
            "needs_ai_edit",
            "needs_ai_fix",
        }
    return {
        "advance_count": state.get("advance_count", 0),
        "accepted_tasks": accepted_answer_count(paths),
        "pending_task_type": pending.get("task_type"),
        "pending_blocks": len(pending.get("blocks", [])),
        "input_pdf": config.get("input_pdf"),
        "config_hash": config.get("config_hash"),
        "asset_dir": config.get("asset_dir"),
        "pipeline_progress": pipeline_progress,
    }


def _record_pipeline_progress(paths, event: dict) -> None:
    event_type = event.get("type")
    if event_type == "stage_summary":
        append_trace(paths, "pipeline_stage_summary", stages=event.get("stages", []))
        return

    state = read_json(paths.state, {}) or {}
    progress = {
        "event_type": event_type,
        "stage": event.get("stage"),
        "stage_progress": event.get("stage_progress"),
        "stage_current": event.get("stage_current"),
        "stage_total": event.get("stage_total"),
        "overall_progress": event.get("overall_progress"),
        "part_index": event.get("part_index"),
        "total_parts": event.get("total_parts"),
        "paused_for_ai": event_type == "progress_paused",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if state:
        progress["paused_for_ai"] = state.get("status") in {
            "needs_ai_edit",
            "needs_ai_fix",
        }
    _write_progress_snapshot(paths, progress)
    if event_type in {"progress_start", "progress_end", "progress_paused"}:
        append_trace(paths, "pipeline_progress", **progress)


def _write_progress_snapshot(paths, progress: dict) -> None:
    try:
        write_json(paths.progress, progress)
    except Exception as exc:
        logger.debug("failed to write progress snapshot", exc_info=True)
        try:
            append_trace(paths, "pipeline_progress_write_failed", error=str(exc))
        except Exception:
            logger.debug("failed to trace progress write failure", exc_info=True)


def _terminal_pipeline_progress() -> dict:
    return {
        "event_type": "done",
        "stage": "Done",
        "stage_progress": 100.0,
        "stage_current": 1,
        "stage_total": 1,
        "overall_progress": 100.0,
        "part_index": None,
        "total_parts": None,
        "paused_for_ai": False,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def shutdown_file_task_runtime() -> None:
    try:
        close_process_pool()
    except Exception:
        logger.debug("failed to close BabelDOC process pool", exc_info=True)
    gc.collect()


def _config_drift_error(state: dict, current_config: dict) -> str | None:
    frozen_hash = (state.get("config") or {}).get("config_hash")
    if not frozen_hash:
        return (
            "state was created without a frozen config; remove .pdf_translate and "
            "run advance with pdf_translate.yaml"
        )
    if frozen_hash != current_config["config_hash"]:
        return (
            "pdf_translate.yaml changed after initialization; restore the original "
            "config or remove .pdf_translate to start a new translation"
        )
    return None


def _config_error_response(paths, error: str, state: dict | None) -> dict:
    if state is not None:
        state["status"] = "config_error"
        state["last_error"] = error
        write_json(paths.state, state)
    append_trace(paths, "config_error", error=error)
    return {
        "status": "config_error",
        "editable_file": None,
        "instruction": "Fix pdf_translate.yaml before running advance again.",
        "progress": _progress(paths, state or {}),
        "validation_errors": [error],
        "validation_warnings": [],
        "trace_tail": trace_tail(paths),
        "output_pdf": _primary_output_pdf(state or {}),
        "output_pdfs": (state or {}).get("output_pdfs", {}),
    }


def _asset_error_response(paths, error: str, state: dict | None) -> dict:
    if state is not None:
        state["status"] = "asset_error"
        state["last_error"] = error
        write_json(paths.state, state)
    append_trace(paths, "asset_error", error=error)
    return {
        "status": "asset_error",
        "editable_file": None,
        "instruction": (
            "Prepare the configured asset_dir with scripts/download_assets.py, "
            "then run advance again."
        ),
        "progress": _progress(paths, state or {}),
        "validation_errors": [error],
        "validation_warnings": [],
        "trace_tail": trace_tail(paths),
        "output_pdf": _primary_output_pdf(state or {}),
        "output_pdfs": (state or {}).get("output_pdfs", {}),
    }


def _lock_error_response(paths, error) -> dict:
    state = read_json(paths.state, {}) or {}
    return {
        "status": "locked",
        "editable_file": str(paths.current_translation)
        if paths.current_translation.exists()
        else None,
        "instruction": "Another advance process is running. Wait for it to finish, then run advance again.",
        "progress": _progress(paths, state),
        "validation_errors": [str(error)],
        "validation_warnings": [],
        "trace_tail": trace_tail(paths),
        "output_pdf": _primary_output_pdf(state),
        "output_pdfs": state.get("output_pdfs", {}),
    }


def _primary_output_pdf(state: dict) -> str | None:
    output_pdfs = state.get("output_pdfs") or {}
    if not output_pdfs:
        return None
    config = state.get("config") or {}
    if config:
        primary = output_pdfs.get(_primary_output_key(config))
        if primary:
            return primary
    return next(iter(output_pdfs.values()), None)


def _collect_output_pdfs(result, config: dict) -> tuple[dict[str, str], str | None]:
    output_pdfs: dict[str, str] = {}
    output_modes = (
        ("mono", "dual") if config["output_mode"] == "both" else (config["output_mode"],)
    )
    watermark_modes = (
        ("watermarked", "no_watermark")
        if config["watermark_output_mode"] == "both"
        else (config["watermark_output_mode"],)
    )
    for watermark in watermark_modes:
        for output_mode in output_modes:
            path = _output_path_for(result, watermark, output_mode)
            if path:
                output_pdfs[f"{watermark}_{output_mode}"] = str(path)
    primary_key = _primary_output_key(config)
    primary = output_pdfs.get(primary_key)
    if primary is not None:
        return output_pdfs, primary
    return output_pdfs, next(iter(output_pdfs.values()), None)


def _validate_output_pdfs(output_pdfs: dict[str, str]) -> list[str]:
    if not output_pdfs:
        return []

    import pymupdf

    errors: list[str] = []
    for label, path_text in output_pdfs.items():
        path = Path(path_text)
        if not path.exists():
            errors.append(f"{label} output PDF is missing: {path}")
            continue
        try:
            document = pymupdf.open(path)
            try:
                text = "\n".join(page.get_text("text") for page in document)
            finally:
                document.close()
        except Exception as exc:
            errors.append(f"{label} output PDF could not be inspected: {exc}")
            continue

        leaked_markers = []
        for match in OUTPUT_INTERNAL_MARKER_RE.finditer(text):
            marker = match.group(0)
            if marker not in leaked_markers:
                leaked_markers.append(marker)
            if len(leaked_markers) >= 5:
                break
        if leaked_markers:
            errors.append(
                f"{label} output PDF leaks internal markers: "
                + ", ".join(leaked_markers)
            )
    return errors


def _output_path_for(result, watermark: str, output_mode: str):
    if watermark == "watermarked":
        return getattr(result, f"{output_mode}_pdf_path")
    no_watermark_path = getattr(result, f"no_watermark_{output_mode}_pdf_path")
    return no_watermark_path or getattr(result, f"{output_mode}_pdf_path")


def _primary_output_key(config: dict) -> str:
    watermark = config["watermark_output_mode"]
    output_mode = config["output_mode"]
    if watermark == "both":
        watermark = "watermarked"
    if output_mode == "both":
        output_mode = "mono"
    return f"{watermark}_{output_mode}"


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
    error_statuses = {"error", "config_error", "asset_error", "locked"}
    return 0 if result.get("status") not in error_statuses else 1
