# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import gc
import json
import logging
import re
import shutil
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
from babeldoc.utils import memory

from .config import ConfigError
from .config import load_workspace_config
from .config import output_flags
from .state import append_trace
from .state import accepted_answer_count
from .state import ensure_page_plan
from .state import load_pending_task
from .state import load_or_init_state
from .state import mark_active_page_completed
from .state import page_at_target_index
from .state import page_is_target
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
        initialized_asset_dir = None
        if existing_state is None:
            try:
                initialized_asset_dir = set_runtime_asset_dir(
                    workspace_config.snapshot["asset_dir"]
                )
            except AssetError as exc:
                return _asset_error_response(paths, str(exc), None)

        state = load_or_init_state(paths, workspace_config.snapshot)
        drift_error = _config_drift_error(state, workspace_config.snapshot)
        if drift_error:
            return _config_error_response(paths, drift_error, state)
        try:
            if _ensure_page_plan(paths, state):
                write_json(paths.state, state)
        except Exception as exc:
            return _config_error_response(paths, str(exc), state)

        state["advance_count"] = int(state.get("advance_count", 0)) + 1
        write_json(paths.state, state)

        validation = validate_pending(paths, state)
        if validation.status in {"needs_ai_edit", "needs_ai_fix"}:
            state = read_json(paths.state, state)
            return _pending_response(paths, state, validation)

        state = read_json(paths.state, state)
        if state.get("status") == "done":
            return _done_response(paths, state, validation.warnings)

        if _page_plan_complete(state):
            try:
                if state["config"]["output_mode"] in {"dual", "both"}:
                    return _advance_dual_assembly(paths, state, validation.warnings)
                return _publish_outputs(paths, state, validation.warnings)
            except Exception as exc:
                return _runtime_error_response(paths, state, exc)

        try:
            if initialized_asset_dir is not None:
                asset_dir = initialized_asset_dir
            else:
                asset_dir = set_runtime_asset_dir(state["config"]["asset_dir"])
        except AssetError as exc:
            return _asset_error_response(paths, str(exc), state)
        append_trace(paths, "assets_ready", asset_dir=str(asset_dir))

        result, response = _run_babeldoc(paths, state)
        if response is not None:
            return response
        try:
            output_pdfs, _output_pdf = _collect_output_pdfs(result, state["config"])
            output_errors = _validate_output_pdfs(output_pdfs)
            if output_errors:
                return _output_error_response(
                    paths,
                    state,
                    output_pdfs,
                    output_errors,
                    validation.warnings,
                )
            active_page = _active_page(state)
            if active_page is None:
                raise RuntimeError("active page disappeared before output commit")
            output_pdfs = _store_page_outputs(paths, state, active_page, output_pdfs)
            mono_shard = output_pdfs.get("mono")
            if mono_shard:
                _apply_mono_page(paths, state, Path(mono_shard), active_page)
            return _mark_active_page_extracted(paths, state, validation.warnings)
        except Exception as exc:
            return _runtime_error_response(paths, state, exc)


def _run_babeldoc(
    paths,
    state: dict,
) -> tuple[object | None, dict | None]:
    translator = FileTaskTranslator(paths, state)
    _record_memory_sample(paths, "load_model_start")
    config = _build_translation_config(paths, state, translator)
    _record_memory_sample(paths, "load_model")
    config.file_task_workflow = True
    config.progress_change_callback = (
        lambda **event: _record_pipeline_progress(paths, event)
    )
    state["status"] = "running"
    write_json(paths.state, state)

    try:
        return translate(config), None
    except FileTaskPending:
        current_state = read_json(paths.state, state)
        return None, _pending_response(
            paths,
            current_state,
            ValidationResult(
                False,
                current_state.get("status", "needs_ai_edit"),
                [],
                [],
            ),
        )
    except Exception as exc:
        return None, _runtime_error_response(paths, state, exc)
    finally:
        shutdown_file_task_runtime()


def _output_error_response(
    paths,
    state: dict,
    output_pdfs: dict[str, str],
    errors: list[str],
    warnings: list[str],
) -> dict:
    state["status"] = "error"
    state["last_error"] = "; ".join(errors)
    state["pending_task_hash"] = None
    write_json(paths.state, state)
    append_trace(paths, "output_validation_error", errors=errors)
    return {
        "status": "error",
        "editable_file": None,
        "instruction": "Fix the output validation errors reported in validation_errors.",
        "progress": _progress(paths, state),
        "validation_errors": errors,
        "validation_warnings": warnings,
        "trace_tail": trace_tail(paths),
        "output_pdf": None,
        "output_pdfs": output_pdfs,
    }


def _runtime_error_response(paths, state: dict, error: Exception) -> dict:
    state["status"] = "error"
    state["last_error"] = str(error)
    write_json(paths.state, state)
    append_trace(
        paths,
        "advance_error",
        error=str(error),
        traceback=traceback.format_exc(limit=8),
    )
    return {
        "status": "error",
        "editable_file": None,
        "instruction": "Fix the runtime error reported in validation_errors.",
        "progress": _progress(paths, state),
        "validation_errors": [str(error)],
        "validation_warnings": [],
        "trace_tail": trace_tail(paths),
        "output_pdf": None,
        "output_pdfs": {},
    }


def _apply_mono_page(paths, state: dict, shard_path: Path, page_number: int) -> None:
    import pymupdf

    assembled_path = paths.assembled / "mono.pdf"
    if not assembled_path.exists():
        temp_path = assembled_path.with_suffix(".tmp.pdf")
        shutil.copyfile(state["config"]["input_pdf"], temp_path)
        temp_path.replace(assembled_path)

    base = pymupdf.open(assembled_path)
    shard = pymupdf.open(shard_path)
    try:
        page_index = page_number - 1
        if page_index < 0 or page_index >= base.page_count:
            raise RuntimeError(
                f"target page {page_number} exceeds output PDF page count "
                f"{base.page_count}"
            )
        if shard.page_count != 1:
            raise RuntimeError(
                f"page shard must contain one page: {shard_path} has "
                f"{shard.page_count}"
            )

        target_xref = base[page_index].xref
        base.insert_pdf(shard, from_page=0, to_page=0)
        inserted_index = base.page_count - 1
        base.xref_copy(
            base[inserted_index].xref,
            target_xref,
            keep=[
                "Parent",
                "Annots",
                "StructParents",
                "Tabs",
                "AA",
                "Dur",
                "Trans",
            ],
        )
        base.delete_page(inserted_index)
        if shard.metadata:
            base.set_metadata(shard.metadata)
        if not base.can_save_incrementally():
            raise RuntimeError("assembled mono PDF cannot be saved incrementally")
        # ponytail: incremental revisions bound each advance; an explicit
        # compaction step can reclaim historical objects when file size matters.
        base.saveIncr()
    finally:
        shard.close()
        base.close()


def _store_page_outputs(
    paths,
    state: dict,
    page_number: int,
    output_pdfs: dict[str, str],
) -> dict[str, str]:
    stored = {}
    for mode, path_text in output_pdfs.items():
        source_path = Path(path_text)
        target_path = _page_output_path(paths, state, page_number, mode)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != target_path.resolve():
            shutil.copyfile(source_path, target_path)
        stored[mode] = str(target_path)
    return stored


def _page_output_path(paths, state: dict, page_number: int, mode: str) -> Path:
    page_name = f"page_{page_number:04d}"
    lang_out = state["config"]["lang_out"]
    return paths.page_outputs / page_name / f"{page_name}.{lang_out}.{mode}.pdf"


def _pdf_page_count(path: Path) -> int:
    if not path.exists():
        return 0
    import pymupdf

    document = pymupdf.open(path)
    try:
        return int(document.page_count)
    finally:
        document.close()


def _append_pdf_page(assembled_path: Path, page_path: Path) -> None:
    import pymupdf

    page = pymupdf.open(page_path)
    try:
        if page.page_count != 1:
            raise RuntimeError(
                f"output page shard must contain one page: {page_path} has "
                f"{page.page_count}"
            )
        if not assembled_path.exists():
            assembled = pymupdf.open()
            temp_path = assembled_path.with_suffix(".tmp.pdf")
            temp_path.unlink(missing_ok=True)
            try:
                assembled.insert_pdf(page)
                assembled.save(temp_path, garbage=1, deflate=True)
            finally:
                assembled.close()
            temp_path.replace(assembled_path)
            return

        assembled = pymupdf.open(assembled_path)
        try:
            assembled.insert_pdf(page)
            if not assembled.can_save_incrementally():
                raise RuntimeError("assembled dual PDF cannot be saved incrementally")
            assembled.saveIncr()
        finally:
            assembled.close()
    finally:
        page.close()


def _append_original_dual_page(assembled_path: Path, page_path: Path) -> None:
    import pymupdf

    source = pymupdf.open(page_path)
    dual_page = pymupdf.open()
    try:
        page = source[0]
        rotation = page.rotation
        width = page.rect.width
        height = page.rect.height
        page.set_rotation(0)
        output_page = dual_page.new_page(width=width * 2, height=height)
        output_page.show_pdf_page(
            pymupdf.Rect(0, 0, width, height),
            source,
            0,
            keep_proportion=True,
            rotate=-rotation,
        )
        output_page.show_pdf_page(
            pymupdf.Rect(width, 0, width * 2, height),
            source,
            0,
            keep_proportion=True,
            rotate=-rotation,
        )

        temp_path = page_path.with_suffix(".dual-original.pdf")
        temp_path.unlink(missing_ok=True)
        dual_page.save(temp_path, garbage=1, deflate=True)
    finally:
        dual_page.close()
        source.close()
    try:
        _append_pdf_page(assembled_path, temp_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _finalize_dual_assembly(paths, state: dict) -> None:
    import pymupdf

    assembled_path = paths.assembled / "dual.pdf"
    document = pymupdf.open(assembled_path)
    source = pymupdf.open(state["config"]["input_pdf"])
    try:
        first_target_page = page_at_target_index(
            state["page_plan"]["target_page_ranges"],
            0,
        )
        if first_target_page is not None:
            shard_path = _page_output_path(
                paths,
                state,
                first_target_page,
                "dual",
            )
            shard = pymupdf.open(shard_path)
            try:
                if shard.metadata:
                    document.set_metadata(shard.metadata)
            finally:
                shard.close()
        toc = source.get_toc()
        if toc:
            document.set_toc(toc)
        if not document.can_save_incrementally():
            raise RuntimeError("completed dual PDF cannot be saved incrementally")
        document.saveIncr()
    finally:
        source.close()
        document.close()


def _mark_active_page_extracted(paths, state: dict, warnings: list[str]) -> dict:
    completed_page, next_page = mark_active_page_completed(state)
    state["pending_task_hash"] = None
    state["output_pdfs"] = state.get("output_pdfs", {})
    state.pop("last_error", None)
    state["status"] = "page_completed"
    state["last_completed_page"] = completed_page
    state["next_page"] = next_page
    write_json(paths.state, state)
    _write_progress_snapshot(
        paths,
        _annotate_progress_with_page_plan(
            _page_completed_pipeline_progress(completed_page),
            state,
        ),
    )
    append_trace(
        paths,
        "page_completed",
        page=completed_page,
        next_page=next_page,
        output_pdfs=state["output_pdfs"],
    )
    if next_page is None and state["config"]["output_mode"] == "mono":
        return _publish_outputs(paths, state, warnings)
    if next_page is None:
        _ensure_output_plan(state)
        write_json(paths.state, state)
    return _page_completed_response(
        paths,
        state,
        completed_page,
        next_page,
        warnings,
    )


def _advance_dual_assembly(paths, state: dict, warnings: list[str]) -> dict:
    plan = _ensure_output_plan(state)
    total = plan["total"]
    completed_count = plan["completed_count"]
    if completed_count >= total:
        if (paths.assembled / "dual.pdf").exists():
            _finalize_dual_assembly(paths, state)
        return _publish_outputs(paths, state, warnings)

    page_number = completed_count + 1
    assembled_path = paths.assembled / "dual.pdf"
    assembled_count = _pdf_page_count(assembled_path)
    if assembled_count == completed_count:
        page_plan = state["page_plan"]
        if page_is_target(page_plan["target_page_ranges"], page_number):
            page_path = _page_output_path(paths, state, page_number, "dual")
            if not page_path.exists():
                return _output_error_response(
                    paths,
                    state,
                    {},
                    [f"dual page shard is missing: {page_path}"],
                    warnings,
                )
            _append_pdf_page(assembled_path, page_path)
        else:
            page_path = _ensure_page_source(paths, state, page_number)
            _append_original_dual_page(assembled_path, page_path)
    elif assembled_count != completed_count + 1:
        return _output_error_response(
            paths,
            state,
            {},
            [
                "dual assembly page count does not match its cursor: "
                f"{assembled_count} pages for cursor {completed_count}"
            ],
            warnings,
        )

    plan["completed_count"] = page_number
    next_page = page_number + 1 if page_number < total else None
    plan["active_page"] = next_page
    state["status"] = "page_completed"
    write_json(paths.state, state)
    _write_progress_snapshot(
        paths,
        _annotate_progress_with_page_plan(
            _output_page_completed_pipeline_progress(page_number),
            state,
        ),
    )
    append_trace(
        paths,
        "output_page_completed",
        page=page_number,
        next_page=next_page,
    )
    if next_page is None:
        _finalize_dual_assembly(paths, state)
        return _publish_outputs(paths, state, warnings)
    return _output_page_completed_response(
        paths,
        state,
        page_number,
        next_page,
        warnings,
    )


def _ensure_output_plan(state: dict) -> dict:
    total = int((state.get("page_plan") or {}).get("source_page_count", 0))
    plan = state.get("output_plan")
    if not isinstance(plan, dict) or plan.get("total") != total:
        plan = {
            "total": total,
            "completed_count": 0,
            "active_page": 1 if total else None,
        }
        state["output_plan"] = plan
    return plan


def _output_page_completed_response(
    paths,
    state: dict,
    page_number: int,
    next_page: int,
    warnings: list[str],
) -> dict:
    return {
        "status": "page_completed",
        "completed_page": state.get("last_completed_page"),
        "next_page": None,
        "finalized_page": page_number,
        "next_finalization_page": next_page,
        "editable_file": None,
        "instruction": (
            f"Output page {page_number} is complete. Run advance again to build "
            f"output page {next_page}."
        ),
        "progress": _progress(paths, state),
        "validation_errors": [],
        "validation_warnings": warnings,
        "trace_tail": trace_tail(paths),
        "output_pdf": None,
        "output_pdfs": {},
    }


def _publish_outputs(paths, state: dict, warnings: list[str]) -> dict:
    config = state["config"]
    output_modes = (
        ("mono", "dual")
        if config["output_mode"] == "both"
        else (config["output_mode"],)
    )
    basename = Path(config["input_pdf"]).stem
    output_pdfs = {}
    missing = []
    publish_paths = {}
    expected_page_count = int(state["page_plan"]["source_page_count"])
    for mode in output_modes:
        assembled_path = paths.assembled / f"{mode}.pdf"
        output_path = paths.output / f"{basename}.{config['lang_out']}.{mode}.pdf"
        if not assembled_path.exists() and not output_path.exists():
            missing.append(f"{mode} output PDF is missing: {assembled_path}")
            continue
        candidate_path = assembled_path if assembled_path.exists() else output_path
        try:
            actual_page_count = _pdf_page_count(candidate_path)
        except Exception as exc:
            missing.append(f"{mode} output PDF could not be inspected: {exc}")
            continue
        if actual_page_count != expected_page_count:
            missing.append(
                f"{mode} output PDF has {actual_page_count} pages; "
                f"expected {expected_page_count}"
            )
            continue
        publish_paths[mode] = (assembled_path, output_path)
    if missing:
        return _output_error_response(paths, state, output_pdfs, missing, warnings)

    for mode, (assembled_path, output_path) in publish_paths.items():
        if assembled_path.exists():
            assembled_path.replace(output_path)
        output_pdfs[mode] = str(output_path)

    state["status"] = "done"
    state["pending_task_hash"] = None
    state["output_pdfs"] = output_pdfs
    state.pop("last_error", None)
    write_json(paths.state, state)
    _write_progress_snapshot(
        paths,
        _annotate_progress_with_page_plan(
            _terminal_pipeline_progress(read_json(paths.progress, None)),
            state,
        ),
    )
    append_trace(
        paths,
        "pdf_written",
        output_pdf=_primary_output_pdf(state),
        output_pdfs=output_pdfs,
    )
    return _done_response(paths, state, warnings)


def _build_translation_config(
    paths,
    state: dict,
    translator: FileTaskTranslator,
) -> TranslationConfig:
    config = state["config"]
    no_dual, no_mono = output_flags(config["output_mode"])
    active_page = _active_page(state)
    input_file = config["input_pdf"]
    page_text = config["pages"]
    output_dir = paths.output
    working_dir = paths.working
    if active_page is not None:
        page_name = f"page_{active_page:04d}"
        input_file = str(_ensure_page_source(paths, state, active_page))
        page_text = "1"
        output_dir = paths.page_outputs / page_name
    return TranslationConfig(
        translator=translator,
        input_file=input_file,
        lang_in=config["lang_in"],
        lang_out=config["lang_out"],
        doc_layout_model=None,
        pages=page_text,
        output_dir=output_dir,
        working_dir=working_dir,
        debug=False,
        no_dual=no_dual,
        no_mono=no_mono,
        use_rich_pbar=False,
        add_formula_placehold_hint=config["add_formula_placehold_hint"],
        primary_font_family=config["primary_font_family"],
        report_interval=2.0,
    )


def _ensure_page_source(paths, state: dict, page_number: int) -> Path:
    import pymupdf

    page_path = paths.page_sources / f"page_{page_number:04d}.pdf"
    if page_path.exists():
        return page_path
    page_path.parent.mkdir(parents=True, exist_ok=True)

    source = pymupdf.open(state["config"]["input_pdf"])
    page = pymupdf.open()
    temp_path = page_path.with_suffix(".tmp.pdf")
    temp_path.unlink(missing_ok=True)
    try:
        page_index = page_number - 1
        if page_index < 0 or page_index >= source.page_count:
            raise RuntimeError(
                f"target page {page_number} exceeds input PDF page count "
                f"{source.page_count}"
            )
        page.insert_pdf(
            source,
            from_page=page_index,
            to_page=page_index,
            links=True,
            annots=True,
        )
        if source.metadata:
            page.set_metadata(source.metadata)
        page.save(temp_path, garbage=1, deflate=True)
    finally:
        page.close()
        source.close()
    temp_path.replace(page_path)
    return page_path


def _ensure_page_plan(paths, state: dict) -> bool:
    input_path = Path(state["config"]["input_pdf"])
    stat = input_path.stat()
    # ponytail: stat identity keeps resumes O(1); add content hashing when
    # workspaces become an untrusted mutation boundary.
    source_identity = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    existing_plan = state.get("page_plan") or {}
    existing_identity = existing_plan.get("source_identity")
    if existing_identity is not None and existing_identity != source_identity:
        raise RuntimeError(
            "input PDF changed after initialization; restore the original PDF or "
            "remove .pdf_translate to start a new translation"
        )
    source_page_count = existing_plan.get("source_page_count")
    if existing_identity is None or not isinstance(source_page_count, int):
        source_page_count = _source_page_count(str(input_path))
    changed = ensure_page_plan(state, source_page_count, source_identity)
    if changed:
        append_trace(
            paths,
            "page_plan_updated",
            page_plan=_public_page_plan(state),
        )
    return changed


def _source_page_count(input_pdf: str) -> int:
    import pymupdf

    document = pymupdf.open(input_pdf)
    try:
        return int(document.page_count)
    finally:
        document.close()


def _active_page(state: dict) -> int | None:
    page_plan = state.get("page_plan") or {}
    active_page = page_plan.get("active_page")
    return active_page if isinstance(active_page, int) else None


def _page_plan_complete(state: dict) -> bool:
    page_plan = state.get("page_plan") or {}
    target_count = page_plan.get("target_count")
    rendered_count = page_plan.get(
        "rendered_count",
        page_plan.get("completed_count"),
    )
    return (
        isinstance(target_count, int)
        and target_count > 0
        and rendered_count == target_count
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
    progress = _progress(paths, state)
    pipeline_progress = progress.get("pipeline_progress")
    if pipeline_progress:
        _write_progress_snapshot(paths, pipeline_progress)
    return {
        "status": status if status != "no_pending" else state.get("status"),
        "editable_file": str(paths.current_translation),
        "instruction": _instruction_for_pending(pending, validation, state),
        "progress": progress,
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
        "trace_tail": trace_tail(paths),
        "output_pdf": _primary_output_pdf(state),
        "output_pdfs": state.get("output_pdfs", {}),
    }


def _done_response(paths, state: dict, warnings: list[str]) -> dict:
    return {
        "status": "done",
        "editable_file": None,
        "instruction": "The translated PDF is complete.",
        "progress": _progress(paths, state),
        "validation_errors": [],
        "validation_warnings": warnings,
        "trace_tail": trace_tail(paths),
        "output_pdf": _primary_output_pdf(state),
        "output_pdfs": state.get("output_pdfs", {}),
    }


def _page_completed_response(
    paths,
    state: dict,
    completed_page: int | None,
    next_page: int | None,
    warnings: list[str],
) -> dict:
    if next_page is None:
        instruction = (
            f"Page {completed_page} is complete. Run advance again to assemble "
            "output page 1."
        )
    else:
        instruction = (
            f"Page {completed_page} is complete. Run advance again to start "
            f"page {next_page}."
        )
    return {
        "status": "page_completed",
        "completed_page": completed_page,
        "next_page": next_page,
        "editable_file": None,
        "instruction": instruction,
        "progress": _progress(paths, state),
        "validation_errors": [],
        "validation_warnings": warnings,
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
    if task_type == "translate":
        body = (
            f"Edit current_translation.yaml. Fill each item's translation field "
            f"with {target_language} text. Keep source fields unchanged. Preserve "
            "every placeholder such as <b1> and </b1> exactly, in the same order."
        )
    else:
        body = (
            "This workspace has an unsupported pending task type. Remove "
            ".pdf_translate to start a fresh translation."
        )
    if validation.errors:
        return body + " Resolve validation_errors and run advance again."
    return body + " Save the file and run advance again."


def _progress(paths, state: dict) -> dict:
    config = state.get("config") or {}
    pending = load_pending_task(paths, state) or {}
    accepted_tasks = state.get("accepted_task_count")
    if not isinstance(accepted_tasks, int):
        accepted_tasks = accepted_answer_count(paths)
    pipeline_progress = read_json(paths.progress, None)
    if state.get("status") == "done":
        pipeline_progress = _terminal_pipeline_progress(pipeline_progress)
    elif pipeline_progress:
        pipeline_progress["paused_for_ai"] = state.get("status") in {
            "needs_ai_edit",
            "needs_ai_fix",
        }
    if pipeline_progress:
        pipeline_progress = _annotate_progress_with_page_plan(
            pipeline_progress,
            state,
        )
    return {
        "advance_count": state.get("advance_count", 0),
        "accepted_tasks": accepted_tasks,
        "pending_task_type": pending.get("task_type"),
        "pending_blocks": len(pending.get("blocks", [])),
        "pending_page": pending.get("page") or state.get("pending_page"),
        "input_pdf": config.get("input_pdf"),
        "config_hash": config.get("config_hash"),
        "asset_dir": config.get("asset_dir"),
        "page_plan": _public_page_plan(state),
        "page_progress": (pipeline_progress or {}).get("page_progress")
        or _page_progress(state, pipeline_progress),
        "pipeline_progress": pipeline_progress,
    }


def _record_pipeline_progress(paths, event: dict) -> None:
    event_type = event.get("type")
    if event_type == "stage_summary":
        append_trace(paths, "pipeline_stage_summary", stages=event.get("stages", []))
        return
    if event_type == "memory_summary":
        state = read_json(paths.state, {}) or {}
        progress = read_json(paths.progress, {}) or {}
        progress["memory_stages"] = event.get("memory_stages", {})
        progress["peak_memory_mb"] = event.get("peak_memory_mb")
        _write_progress_snapshot(paths, progress)
        append_trace(
            paths,
            "pipeline_memory_summary",
            memory_stages=event.get("memory_stages", {}),
            peak_memory_mb=event.get("peak_memory_mb"),
            page_plan=_public_page_plan(state),
        )
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
        progress = _annotate_progress_with_page_plan(progress, state)
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


def _terminal_pipeline_progress(previous: dict | None = None) -> dict:
    progress = {
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
    if previous:
        for key in ("peak_memory_mb", "memory_stages"):
            if key in previous:
                progress[key] = previous[key]
    return progress


def _page_completed_pipeline_progress(completed_page: int | None) -> dict:
    return {
        "event_type": "page_completed",
        "stage": "Page Completed",
        "stage_progress": 100.0,
        "stage_current": 1,
        "stage_total": 1,
        "overall_progress": 100.0,
        "part_index": None,
        "total_parts": None,
        "paused_for_ai": False,
        "completed_page": completed_page,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _public_page_plan(state: dict) -> dict | None:
    page_plan = state.get("page_plan")
    if not isinstance(page_plan, dict):
        return None
    return {
        "source_page_count": page_plan.get("source_page_count"),
        "target_page_ranges": [
            list(page_range)
            for page_range in page_plan.get("target_page_ranges") or []
        ],
        "target_count": page_plan.get("target_count"),
        "active_page": page_plan.get("active_page"),
        "completed_count": page_plan.get("completed_count"),
        "rendered_count": page_plan.get(
            "rendered_count",
            page_plan.get("completed_count"),
        ),
    }


def _output_page_completed_pipeline_progress(completed_page: int) -> dict:
    return {
        "event_type": "output_page_completed",
        "stage": "Output Assembly",
        "stage_progress": 100.0,
        "stage_current": 1,
        "stage_total": 1,
        "overall_progress": 100.0,
        "part_index": None,
        "total_parts": None,
        "paused_for_ai": False,
        "completed_page": completed_page,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _annotate_progress_with_page_plan(
    progress: dict,
    state: dict,
) -> dict:
    annotated = dict(progress)
    annotated["page_plan"] = _public_page_plan(state)
    page_progress = _page_progress(
        state,
        annotated,
    )
    annotated["page_progress"] = page_progress
    if page_progress:
        annotated["active_stage_current"] = progress.get("stage_current")
        annotated["active_stage_total"] = progress.get("stage_total")
        annotated["active_stage_progress"] = progress.get("stage_progress")
        annotated["stage_overall_progress"] = progress.get("overall_progress")
        annotated["workflow_current"] = page_progress.get("workflow_current")
        annotated["workflow_total"] = page_progress.get("workflow_total")
        annotated["workflow_progress"] = page_progress["overall_progress"]
        annotated["overall_progress"] = page_progress["overall_progress"]
    return annotated


def _page_progress(
    state: dict,
    pipeline_progress: dict | None,
) -> dict | None:
    page_plan = state.get("page_plan")
    if not isinstance(page_plan, dict):
        return None
    target_total = int(page_plan.get("target_count") or 0)
    if target_total < 1:
        return None
    completed_count = min(
        max(int(page_plan.get("completed_count") or 0), 0),
        target_total,
    )
    rendered_count = min(
        max(int(page_plan.get("rendered_count", completed_count) or 0), 0),
        target_total,
    )
    active_page = page_plan.get("active_page")
    output_mode = (state.get("config") or {}).get("output_mode")
    output_plan = state.get("output_plan") or {}
    output_total = (
        int(page_plan.get("source_page_count") or 0)
        if output_mode in {"dual", "both"}
        else 0
    )
    output_completed_count = min(
        max(int(output_plan.get("completed_count") or 0), 0),
        output_total,
    )
    if state.get("status") == "done":
        completed_count = target_total
        rendered_count = target_total
        output_completed_count = output_total
        active_page = None
    workflow_current = rendered_count + output_completed_count
    workflow_total = target_total + output_total
    overall = (workflow_current / workflow_total) * 100.0
    return {
        "target_total": target_total,
        "completed_count": completed_count,
        "rendered_count": rendered_count,
        "active_page": active_page,
        "output_total": output_total,
        "output_completed_count": output_completed_count,
        "output_active_page": output_plan.get("active_page"),
        "active_page_stage": (pipeline_progress or {}).get("stage"),
        "active_page_stage_progress": (pipeline_progress or {}).get("stage_progress"),
        "active_page_progress": 0.0,
        "workflow_current": workflow_current,
        "workflow_total": workflow_total,
        "overall_progress": round(overall, 2),
    }


def shutdown_file_task_runtime() -> None:
    try:
        close_process_pool()
    except Exception:
        logger.debug("failed to close BabelDOC process pool", exc_info=True)
    gc.collect()


def _record_memory_sample(paths, stage: str) -> None:
    try:
        value, _ = memory.get_memory_usage_with_throttle(
            include_children=True,
            prefer_pss=True,
        )
        peak_memory_mb = round(value / (1024 * 1024), 2)
    except Exception as exc:
        logger.debug("failed to record memory sample", exc_info=True)
        append_trace(paths, "memory_sample_error", stage=stage, error=str(exc))
        return
    append_trace(paths, "memory_sample", stage=stage, peak_memory_mb=peak_memory_mb)


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
    for output_mode in output_modes:
        path = getattr(result, f"{output_mode}_pdf_path")
        if path:
            output_pdfs[output_mode] = str(path)
    primary_key = _primary_output_key(config)
    primary = output_pdfs.get(primary_key)
    if primary is not None:
        return output_pdfs, primary
    return output_pdfs, next(iter(output_pdfs.values()), None)


def _validate_output_pdfs(output_pdfs: dict[str, str]) -> list[str]:
    if not output_pdfs:
        return ["output PDF was not generated"]

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
                leaked_markers = []
                for page in document:
                    for match in OUTPUT_INTERNAL_MARKER_RE.finditer(
                        page.get_text("text")
                    ):
                        marker = match.group(0)
                        if marker not in leaked_markers:
                            leaked_markers.append(marker)
                        if len(leaked_markers) >= 5:
                            break
                    if len(leaked_markers) >= 5:
                        break
            finally:
                document.close()
        except Exception as exc:
            errors.append(f"{label} output PDF could not be inspected: {exc}")
            continue

        if leaked_markers:
            errors.append(
                f"{label} output PDF leaks internal markers: "
                + ", ".join(leaked_markers)
            )
    return errors


def _primary_output_key(config: dict) -> str:
    output_mode = config["output_mode"]
    if output_mode == "both":
        output_mode = "mono"
    return output_mode


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
