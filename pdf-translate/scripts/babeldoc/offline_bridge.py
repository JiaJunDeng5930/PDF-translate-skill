from __future__ import annotations


class OfflineTranslationPending(RuntimeError):
    """Raised when BabelDOC reaches an AI task that must be edited on disk."""


def is_offline_file_workflow(translation_config) -> bool:
    return bool(getattr(translation_config, "offline_file_workflow", False))


class ImmediateExecutor:
    """Executor-compatible adapter used by the offline one-task workflow."""

    def submit(self, fn, *args, **kwargs):
        kwargs.pop("priority", None)
        return fn(*args, **kwargs)
