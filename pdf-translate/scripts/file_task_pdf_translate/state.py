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
    if not paths.trace.exists():
        return []
    lines = paths.trace.read_text(encoding="utf-8").splitlines()[-limit:]
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def default_state(config_snapshot: dict) -> dict:
    return {
        "version": STATE_VERSION,
        "config": config_snapshot,
        "pending_task_hash": None,
        "status": "initialized",
        "output_pdfs": {},
        "advance_count": 0,
    }


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
    write_json(answer_file, answer)
    answer_hash = stable_hash(answer)
    state["pending_task_hash"] = None
    state["status"] = "running"
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
