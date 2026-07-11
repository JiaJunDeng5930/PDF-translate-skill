# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .editable import EditableBlock
from .editable import render_editable_document

STATE_VERSION = 1
LOCK_STALE_SECONDS = 6 * 60 * 60


@dataclass
class WorkspacePaths:
    root: Path
    private: Path
    state: Path
    progress: Path
    trace: Path
    config: Path
    current_translation: Path
    tasks: Path
    accepted: Path
    rejected: Path
    output: Path
    working: Path
    page_sources: Path
    page_outputs: Path
    assembled: Path
    lock: Path


class WorkspaceLockError(RuntimeError):
    def __init__(self, lock_path: Path, metadata: dict | None):
        self.lock_path = lock_path
        self.metadata = metadata or {}
        pid = self.metadata.get("pid")
        detail = f" held by pid {pid}" if pid else ""
        super().__init__(f"advance lock exists: {lock_path}{detail}")


def paths_for(root: Path) -> WorkspacePaths:
    private = root / ".pdf_translate"
    return WorkspacePaths(
        root=root,
        private=private,
        state=private / "state.json",
        progress=private / "progress.json",
        trace=private / "trace.jsonl",
        config=root / "pdf_translate.yaml",
        current_translation=root / "current_translation.yaml",
        tasks=private / "tasks",
        accepted=private / "accepted_answers",
        rejected=private / "rejected_answers",
        output=root / "output",
        working=private / "babeldoc_work",
        page_sources=private / "page_sources",
        page_outputs=private / "page_outputs",
        assembled=private / "assembled",
        lock=private / "advance.lock",
    )


def ensure_dirs(paths: WorkspacePaths) -> None:
    for path in (
        paths.private,
        paths.tasks,
        paths.accepted,
        paths.rejected,
        paths.output,
        paths.working,
        paths.page_sources,
        paths.page_outputs,
        paths.assembled,
    ):
        path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def stable_hash(data) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def append_trace(paths: WorkspacePaths, event: str, **fields) -> None:
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    with paths.trace.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def trace_tail(paths: WorkspacePaths, limit: int = 5) -> list[dict]:
    if limit < 1 or not paths.trace.exists():
        return []
    with paths.trace.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= limit:
            read_size = min(8192, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data
    lines = data.splitlines()
    if position > 0 and lines:
        lines = lines[1:]
    result = []
    for line in lines[-limit:]:
        try:
            result.append(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return result


def default_state(config_snapshot: dict) -> dict:
    return {
        "version": STATE_VERSION,
        "config": config_snapshot,
        "pending_task_hash": None,
        "status": "initialized",
        "output_pdfs": {},
        "accepted_task_count": 0,
        "advance_count": 0,
    }


def parse_page_ranges(pages: str | None, source_page_count: int) -> list[list[int]]:
    if source_page_count < 1:
        raise RuntimeError("input PDF has no pages")
    if pages is None or not str(pages).strip():
        return [[1, source_page_count]]

    result: list[list[int]] = []
    for raw_part in str(pages).split(","):
        part = raw_part.strip()
        if not part:
            raise RuntimeError("pages contains an empty range")
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2:
                raise RuntimeError(f"invalid pages range: {part}")
            start_text, end_text = pieces
            try:
                start = int(start_text) if start_text else 1
                end = int(end_text) if end_text else source_page_count
            except ValueError as exc:
                raise RuntimeError(f"invalid pages range: {part}") from exc
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise RuntimeError(f"invalid pages page number: {part}") from exc
        if start < 1 or end < 1 or start > end:
            raise RuntimeError(f"invalid pages range: {part}")
        if end > source_page_count:
            raise RuntimeError(
                f"pages range {part} exceeds input PDF page count {source_page_count}"
            )

        pending = [(start, end)]
        for covered_start, covered_end in result:
            uncovered = []
            for pending_start, pending_end in pending:
                if pending_end < covered_start or covered_end < pending_start:
                    uncovered.append((pending_start, pending_end))
                    continue
                if pending_start < covered_start:
                    uncovered.append((pending_start, covered_start - 1))
                if covered_end < pending_end:
                    uncovered.append((covered_end + 1, pending_end))
            pending = uncovered
        result.extend([[range_start, range_end] for range_start, range_end in pending])
    if not result:
        raise RuntimeError("pages selects no input PDF pages")
    return result


def page_at_target_index(page_ranges: list[list[int]], index: int) -> int | None:
    if index < 0:
        return None
    for start, end in page_ranges:
        range_size = end - start + 1
        if index < range_size:
            return start + index
        index -= range_size
    return None


def page_is_target(page_ranges: list[list[int]], page: int) -> bool:
    return any(start <= page <= end for start, end in page_ranges)


def active_batch_pages(state: dict) -> list[int]:
    plan = state.get("page_plan") or {}
    ranges = plan.get("target_page_ranges") or []
    if not ranges:
        active_page = plan.get("active_page")
        return [active_page] if isinstance(active_page, int) else []
    start_index = int(plan.get("rendered_count", plan.get("completed_count", 0)))
    target_count = int(plan.get("target_count", 0))
    batch_size = int((state.get("config") or {}).get("pages_per_advance", 1))
    pages = []
    for index in range(start_index, min(start_index + batch_size, target_count)):
        page = page_at_target_index(ranges, index)
        if page is None or (pages and page != pages[-1] + 1):
            break
        pages.append(page)
    return pages


def mark_active_batch_completed(state: dict) -> tuple[list[int], int | None]:
    pages = active_batch_pages(state)
    if not pages:
        return [], None

    plan = state["page_plan"]
    target_count = int(plan.get("target_count", 0))
    rendered_count = min(
        int(plan.get("rendered_count", plan.get("completed_count", 0))) + len(pages),
        target_count,
    )
    plan["rendered_count"] = rendered_count
    plan["completed_count"] = max(
        int(plan.get("completed_count", 0)),
        rendered_count,
    )
    next_page = page_at_target_index(
        plan.get("target_page_ranges") or [],
        rendered_count,
    )
    plan["active_page"] = next_page
    return pages, next_page


def ensure_page_plan(
    state: dict,
    source_page_count: int,
    source_identity: dict | None = None,
) -> bool:
    target_page_ranges = parse_page_ranges(
        (state.get("config") or {}).get("pages"),
        source_page_count,
    )
    target_count = sum(end - start + 1 for start, end in target_page_ranges)
    plan = state.get("page_plan")
    if not isinstance(plan, dict):
        plan = {}

    same_selection = plan.get("target_page_ranges") == target_page_ranges
    if same_selection:
        completed_count = plan.get("completed_count")
        if not isinstance(completed_count, int):
            completed_count = 0
        rendered_count = plan.get("rendered_count")
        if not isinstance(rendered_count, int):
            rendered_count = completed_count
    else:
        completed_count = 0
        rendered_count = 0
        legacy_target_pages = plan.get("target_pages")
        legacy_selection_matches = (
            isinstance(legacy_target_pages, list)
            and len(legacy_target_pages) == target_count
            and all(
                page == page_at_target_index(target_page_ranges, index)
                for index, page in enumerate(legacy_target_pages)
            )
        )
        if legacy_selection_matches:
            for index, page in enumerate(plan.get("completed_pages") or []):
                if page != page_at_target_index(target_page_ranges, index):
                    break
                completed_count += 1
    completed_count = min(max(completed_count, 0), target_count)
    rendered_count = min(max(rendered_count, 0), target_count)
    if state.get("status") == "done":
        completed_count = target_count
        rendered_count = target_count

    compact_plan = {
        "source_page_count": source_page_count,
        "target_page_ranges": target_page_ranges,
        "target_count": target_count,
        "active_page": page_at_target_index(target_page_ranges, rendered_count),
        "completed_count": completed_count,
        "rendered_count": rendered_count,
    }
    if source_identity is not None:
        compact_plan["source_identity"] = source_identity
    if state.get("status") == "done":
        compact_plan["active_page"] = None
    if compact_plan == plan:
        return False
    state["page_plan"] = compact_plan
    return True


def load_or_init_state(
    paths: WorkspacePaths,
    config_snapshot: dict | None = None,
) -> dict:
    ensure_dirs(paths)
    state = read_json(paths.state, None)
    if state is not None:
        return state
    if config_snapshot is None:
        raise RuntimeError("config snapshot is required to initialize state")
    state = default_state(config_snapshot)
    write_json(paths.state, state)
    append_trace(
        paths,
        "state_initialized",
        input_pdf=config_snapshot["input_pdf"],
        config_hash=config_snapshot["config_hash"],
        config=config_snapshot,
    )
    return state


def pending_snapshot_path(paths: WorkspacePaths, task_hash: str) -> Path:
    return paths.tasks / f"{task_hash}.snapshot.json"


def load_pending_task(paths: WorkspacePaths, state: dict) -> dict | None:
    task_hash = state.get("pending_task_hash")
    if not task_hash:
        return None
    return read_json(pending_snapshot_path(paths, task_hash), None)


def answer_path(paths: WorkspacePaths, task_hash: str) -> Path:
    return paths.accepted / f"{task_hash}.answer.json"


def save_pending_task(
    paths: WorkspacePaths,
    state: dict,
    snapshot: dict,
    blocks: list[EditableBlock],
) -> None:
    task_hash = snapshot["task_hash"]
    state["pending_task_hash"] = task_hash
    state["status"] = "needs_ai_edit"
    if snapshot.get("page") is not None:
        state["pending_page"] = snapshot["page"]
    state.pop("last_error", None)
    write_json(pending_snapshot_path(paths, task_hash), snapshot)
    atomic_write_text(
        paths.current_translation,
        render_editable_document(
            snapshot["task_type"],
            blocks,
            snapshot.get("lang_out"),
        ),
    )
    write_json(paths.state, state)
    append_trace(
        paths,
        "pending_task_created",
        task_hash=task_hash,
        task_type=snapshot["task_type"],
        page=snapshot.get("page"),
        block_count=len(blocks),
    )


def archive_current_translation(paths: WorkspacePaths, task_hash: str, accepted: bool):
    if not paths.current_translation.exists():
        return
    target_dir = paths.accepted if accepted else paths.rejected
    suffix = "accepted" if accepted else "rejected"
    target = target_dir / f"{task_hash}.{suffix}.yaml"
    shutil.copy2(paths.current_translation, target)


def save_accepted_answer(
    paths: WorkspacePaths,
    state: dict,
    task_hash: str,
    answer,
    summary: dict | None = None,
):
    answer_file = answer_path(paths, task_hash)
    answer_is_new = not answer_file.exists()
    accepted_count = state.get("accepted_task_count")
    if not isinstance(accepted_count, int):
        accepted_count = accepted_answer_count(paths)
    write_json(answer_file, answer)
    answer_hash = stable_hash(answer)
    state["pending_task_hash"] = None
    state.pop("pending_page", None)
    state["status"] = "running"
    state["accepted_task_count"] = accepted_count + int(answer_is_new)
    state.pop("last_error", None)
    write_json(paths.state, state)
    append_trace(
        paths,
        "answer_accepted",
        task_hash=task_hash,
        answer_hash=answer_hash,
        **(summary or {}),
    )


def load_accepted_answer(paths: WorkspacePaths, state: dict, task_hash: str):
    return read_json(answer_path(paths, task_hash), None)


def accepted_answer_count(paths: WorkspacePaths) -> int:
    if not paths.accepted.exists():
        return 0
    return sum(1 for _path in paths.accepted.glob("*.answer.json"))


def _new_lock_metadata(paths: WorkspacePaths, pid: int | None = None) -> dict:
    now = time.time()
    return {
        "pid": pid or os.getpid(),
        "created_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "created_at_epoch": now,
        "workspace": str(paths.root),
    }


def write_lock_metadata(paths: WorkspacePaths, pid: int | None = None) -> dict:
    ensure_dirs(paths)
    metadata = _new_lock_metadata(paths, pid)
    write_json(paths.lock, metadata)
    return metadata


def _read_lock_metadata(lock_path: Path) -> dict | None:
    try:
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return metadata if isinstance(metadata, dict) else None


def _process_exists(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def _lock_is_stale(paths: WorkspacePaths, metadata: dict | None) -> bool:
    if metadata:
        pid = metadata.get("pid")
        if isinstance(pid, int):
            return not _process_exists(pid)

        created_at_epoch = metadata.get("created_at_epoch")
        if isinstance(created_at_epoch, (int, float)):
            return time.time() - float(created_at_epoch) > LOCK_STALE_SECONDS

    try:
        lock_age = time.time() - paths.lock.stat().st_mtime
    except FileNotFoundError:
        return True
    return lock_age > LOCK_STALE_SECONDS


def _write_lock_metadata_to_fd(
    fd: int,
    paths: WorkspacePaths,
) -> dict:
    metadata = _new_lock_metadata(paths)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return metadata


@contextmanager
def workspace_lock(paths: WorkspacePaths) -> Iterator[WorkspaceLockError | None]:
    ensure_dirs(paths)
    while True:
        try:
            fd = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            _write_lock_metadata_to_fd(fd, paths)
            break
        except FileExistsError:
            metadata = _read_lock_metadata(paths.lock)
            if _lock_is_stale(paths, metadata):
                stale_metadata = metadata or {}
                paths.lock.unlink(missing_ok=True)
                append_trace(
                    paths,
                    "stale_lock_recovered",
                    lock_path=str(paths.lock),
                    lock_metadata=stale_metadata,
                )
                continue
            yield WorkspaceLockError(paths.lock, metadata)
            return
    try:
        yield None
    finally:
        try:
            paths.lock.unlink()
        except FileNotFoundError:
            pass
