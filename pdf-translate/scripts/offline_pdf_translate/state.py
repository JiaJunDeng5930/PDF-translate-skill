from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .editable import EditableBlock
from .editable import render_blocks

STATE_VERSION = 1


@dataclass
class WorkspacePaths:
    root: Path
    private: Path
    state: Path
    trace: Path
    current_translation: Path
    tasks: Path
    accepted: Path
    rejected: Path
    output: Path
    working: Path
    lock: Path


def paths_for(root: Path) -> WorkspacePaths:
    private = root / ".pdf_translate"
    return WorkspacePaths(
        root=root,
        private=private,
        state=private / "state.json",
        trace=private / "trace.jsonl",
        current_translation=root / "current_translation.txt",
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
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


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


def find_single_pdf(root: Path) -> Path:
    pdfs = sorted(
        p for p in root.glob("*.pdf") if p.is_file() and not p.name.startswith("~")
    )
    if len(pdfs) != 1:
        names = [p.name for p in pdfs]
        raise RuntimeError(
            "workspace must contain exactly one source PDF before the first advance; "
            f"found {len(pdfs)}: {names}"
        )
    return pdfs[0]


def default_state(input_pdf: Path) -> dict:
    return {
        "version": STATE_VERSION,
        "input_pdf": str(input_pdf),
        "lang_in": "English",
        "lang_out": "zh-CN",
        "accepted": {},
        "pending": None,
        "status": "initialized",
        "output_pdf": None,
        "advance_count": 0,
    }


def load_or_init_state(paths: WorkspacePaths) -> dict:
    ensure_dirs(paths)
    state = read_json(paths.state, None)
    if state is not None:
        return state
    input_pdf = find_single_pdf(paths.root)
    state = default_state(input_pdf)
    write_json(paths.state, state)
    append_trace(paths, "state_initialized", input_pdf=str(input_pdf))
    return state


def save_pending_task(
    paths: WorkspacePaths,
    state: dict,
    snapshot: dict,
    blocks: list[EditableBlock],
) -> None:
    task_hash = snapshot["task_hash"]
    state["pending"] = snapshot
    state["status"] = "needs_ai_edit"
    write_json(paths.tasks / f"{task_hash}.snapshot.json", snapshot)
    atomic_write_text(paths.current_translation, render_blocks(blocks))
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
    target = target_dir / f"{task_hash}.{suffix}.txt"
    shutil.copy2(paths.current_translation, target)


def save_accepted_answer(paths: WorkspacePaths, state: dict, task_hash: str, answer):
    answer_file = paths.accepted / f"{task_hash}.answer.json"
    write_json(answer_file, answer)
    state["accepted"][task_hash] = {"answer_file": str(answer_file)}
    state["pending"] = None
    state["status"] = "running"
    write_json(paths.state, state)
    append_trace(paths, "answer_accepted", task_hash=task_hash)


def load_accepted_answer(paths: WorkspacePaths, state: dict, task_hash: str):
    entry = state.get("accepted", {}).get(task_hash)
    if not entry:
        return None
    return read_json(Path(entry["answer_file"]), None)


@contextmanager
def workspace_lock(paths: WorkspacePaths) -> Iterator[None]:
    ensure_dirs(paths)
    try:
        fd = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError as exc:
        raise RuntimeError(f"advance lock exists: {paths.lock}") from exc
    try:
        yield
    finally:
        try:
            paths.lock.unlink()
        except FileNotFoundError:
            pass
