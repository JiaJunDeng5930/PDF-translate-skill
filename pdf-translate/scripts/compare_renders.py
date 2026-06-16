# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
PAGE_RE = re.compile(r"(?:page[-_]?|^)(\d+)", re.IGNORECASE)


def page_number(path: Path) -> int:
    match = PAGE_RE.search(path.stem)
    if not match:
        raise ValueError(f"cannot find page number in {path.name}")
    return int(match.group(1))


def render_files(directory: Path) -> dict[int, Path]:
    files = {}
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            files[page_number(path)] = path
    return dict(sorted(files.items()))


def digest(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hash_.update(chunk)
    return hash_.hexdigest()


def compare_renders(source_dir: Path, target_dir: Path) -> dict:
    source = render_files(source_dir)
    target = render_files(target_dir)
    pages = sorted(set(source) & set(target))
    changed_pages = [
        page for page in pages if digest(source[page]) != digest(target[page])
    ]
    return {
        "compared_pages": pages,
        "changed_pages": changed_pages,
        "missing_in_source": sorted(set(target) - set(source)),
        "missing_in_target": sorted(set(source) - set(target)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("target_dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            compare_renders(args.source_dir, args.target_dir),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
