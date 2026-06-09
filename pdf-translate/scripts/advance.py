from __future__ import annotations

import sys
from pathlib import Path


def _add_local_imports() -> None:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def main() -> int:
    _add_local_imports()
    from file_task_pdf_translate.runner import main as runner_main

    return runner_main()


if __name__ == "__main__":
    raise SystemExit(main())
