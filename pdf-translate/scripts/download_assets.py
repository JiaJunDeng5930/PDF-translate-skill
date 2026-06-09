# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def _add_local_imports() -> None:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def main(argv: list[str] | None = None) -> int:
    _add_local_imports()

    from babeldoc.assets.assets import AssetError
    from babeldoc.assets.assets import download_runtime_assets

    parser = argparse.ArgumentParser(
        description="Download pdf-translate runtime assets into a local directory.",
    )
    parser.add_argument(
        "asset_dir",
        help="Directory that will contain manifest.json, models, fonts, cmap, and tiktoken assets.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stdout,
    )

    try:
        asset_dir = download_runtime_assets(Path(args.asset_dir))
    except AssetError as exc:
        print(json.dumps({"status": "asset_error", "error": str(exc)}, indent=2))
        return 1

    print(
        json.dumps(
            {
                "status": "done",
                "asset_dir": str(asset_dir),
                "manifest": str(asset_dir / "manifest.json"),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
