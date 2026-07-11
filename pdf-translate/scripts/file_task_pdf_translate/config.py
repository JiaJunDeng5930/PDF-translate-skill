# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 JiajunDeng

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


CONFIG_FILE_NAME = "pdf_translate.yaml"
OUTPUT_MODES = {"mono", "dual", "both"}
PRIMARY_FONT_FAMILIES = {None, "serif", "sans-serif", "script"}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    snapshot: dict

    @property
    def config_hash(self) -> str:
        return self.snapshot["config_hash"]


def load_workspace_config(root: Path) -> WorkspaceConfig:
    root = root.resolve()
    config_path = root / CONFIG_FILE_NAME
    if not config_path.exists():
        raise ConfigError(
            f"{CONFIG_FILE_NAME} is required before the first advance: {config_path}"
        )

    data = _load_yaml(config_path)
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_FILE_NAME} must contain a mapping")

    snapshot = _normalize_config(root, data)
    return WorkspaceConfig(path=config_path, snapshot=snapshot)


def output_flags(output_mode: str) -> tuple[bool, bool]:
    if output_mode == "mono":
        return True, False
    if output_mode == "dual":
        return False, True
    return False, False


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required to read pdf_translate.yaml") from exc

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"cannot parse {CONFIG_FILE_NAME}: {exc}") from exc


def _normalize_config(root: Path, data: dict) -> dict:
    errors: list[str] = []
    if "version" in data:
        errors.append("version is not a translation setting; remove version")
    if "table_model" in data:
        errors.append("table_model is not a translation setting; remove table_model")
    if "watermark_output_mode" in data:
        errors.append(
            "remove watermark_output_mode; outputs are clean by default"
        )

    input_pdf = data.get("input_pdf")
    if not input_pdf:
        errors.append("input_pdf is required")
        input_path = None
    elif not isinstance(input_pdf, str):
        errors.append("input_pdf must be a string")
        input_path = None
    else:
        input_path = Path(input_pdf)
        if not input_path.is_absolute():
            input_path = root / input_path
        input_path = input_path.resolve()
        if not input_path.is_file():
            errors.append(f"input_pdf does not exist: {input_path}")

    lang_in = _required_string(data, "lang_in", errors)
    lang_out = _required_string(data, "lang_out", errors)
    asset_dir_value = _required_string(data, "asset_dir", errors)
    if asset_dir_value:
        asset_dir = Path(asset_dir_value)
        if not asset_dir.is_absolute():
            asset_dir = root / asset_dir
        asset_dir = asset_dir.resolve()
    else:
        asset_dir = None

    pages = data.get("pages")
    if pages is not None and not isinstance(pages, str):
        errors.append("pages must be null or a string")

    pages_per_advance = data.get("pages_per_advance", 1)
    if (
        isinstance(pages_per_advance, bool)
        or not isinstance(pages_per_advance, int)
        or pages_per_advance < 1
    ):
        errors.append("pages_per_advance must be a positive integer")

    output_mode = data.get("output_mode", "mono")
    if output_mode not in OUTPUT_MODES:
        errors.append("output_mode must be one of: mono, dual, both")

    primary_font_family = data.get("primary_font_family")
    if primary_font_family not in PRIMARY_FONT_FAMILIES:
        errors.append("primary_font_family must be null, serif, sans-serif, or script")

    add_formula_placehold_hint = data.get("add_formula_placehold_hint", True)
    if not isinstance(add_formula_placehold_hint, bool):
        errors.append("add_formula_placehold_hint must be true or false")

    if errors:
        raise ConfigError("; ".join(errors))

    snapshot = {
        "input_pdf": str(input_path),
        "lang_in": lang_in,
        "lang_out": lang_out,
        "asset_dir": str(asset_dir),
        "pages": pages,
        "output_mode": output_mode,
        "primary_font_family": primary_font_family,
        "add_formula_placehold_hint": add_formula_placehold_hint,
    }
    if "pages_per_advance" in data:
        snapshot["pages_per_advance"] = pages_per_advance
    snapshot["config_hash"] = _stable_hash(snapshot)
    return snapshot


def _required_string(data: dict, key: str, errors: list[str]) -> str | None:
    value = data.get(key)
    if not value:
        errors.append(f"{key} is required")
        return None
    if not isinstance(value, str):
        errors.append(f"{key} must be a string")
        return None
    return value


def _stable_hash(data: dict) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]
