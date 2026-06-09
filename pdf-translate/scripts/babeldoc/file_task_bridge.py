from __future__ import annotations


class FileTaskPending(RuntimeError):
    """Raised when BabelDOC reaches an AI task that must be edited on disk."""


def is_file_task_workflow(translation_config) -> bool:
    return bool(getattr(translation_config, "file_task_workflow", False))


class ImmediateExecutor:
    """Executor-compatible adapter used by the file-task one-task workflow."""

    def submit(self, fn, *args, **kwargs):
        kwargs.pop("priority", None)
        return fn(*args, **kwargs)
