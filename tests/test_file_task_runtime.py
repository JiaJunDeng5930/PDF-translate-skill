from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "pdf-translate" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def write_test_pdf(path: Path, texts: list[str] | None = None) -> None:
    import pymupdf

    page_texts = texts or ["test page"]
    doc = pymupdf.open()
    try:
        for text in page_texts:
            page = doc.new_page(width=300, height=160)
            page.insert_text((30, 80), text)
        doc.save(path)
    finally:
        doc.close()


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (self.tmp / "assets").mkdir()

    def write_config(self, text: str) -> None:
        (self.tmp / "pdf_translate.yaml").write_text(text, encoding="utf-8")

    def test_load_config_requires_workspace_config(self) -> None:
        from file_task_pdf_translate.config import ConfigError
        from file_task_pdf_translate.config import load_workspace_config

        with self.assertRaises(ConfigError) as ctx:
            load_workspace_config(self.tmp)

        self.assertIn("pdf_translate.yaml", str(ctx.exception))

    def test_config_reports_required_fields_and_invalid_values(self) -> None:
        from file_task_pdf_translate.config import ConfigError
        from file_task_pdf_translate.config import load_workspace_config

        self.write_config(
            """
version: 1
output_mode: booklet
watermark_output_mode: clean
primary_font_family: display
table_model: unsupported
""".strip()
            + "\n",
        )

        with self.assertRaises(ConfigError) as ctx:
            load_workspace_config(self.tmp)

        message = str(ctx.exception)
        self.assertIn("input_pdf is required", message)
        self.assertIn("lang_in is required", message)
        self.assertIn("lang_out is required", message)
        self.assertIn("asset_dir is required", message)
        self.assertIn("version is not a translation setting", message)
        self.assertIn("output_mode must be one of", message)
        self.assertIn("remove watermark_output_mode", message)
        self.assertIn("primary_font_family must be", message)
        self.assertIn("table_model is not a translation setting", message)

    def test_config_maps_language_output_to_babeldoc_params(self) -> None:
        from file_task_pdf_translate.config import load_workspace_config
        from file_task_pdf_translate.runner import _build_translation_config
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.translator import FileTaskTranslator

        self.write_config(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: ja
asset_dir: assets
pages: "2-3"
output_mode: dual
primary_font_family: serif
add_formula_placehold_hint: false
""".strip()
            + "\n",
        )
        snapshot = load_workspace_config(self.tmp).snapshot
        self.assertNotIn("babeldoc_config", snapshot)
        state = {
            "config": snapshot,
            "pending_task_hash": None,
            "status": "initialized",
        }
        translator = FileTaskTranslator(paths_for(self.tmp), state)
        with patch(
            "babeldoc.docvision.doclayout.OnnxModel.load_available",
            return_value=object(),
        ):
            config = _build_translation_config(paths_for(self.tmp), state, translator)

        self.assertEqual(translator.lang_in, "en")
        self.assertEqual(translator.lang_out, "ja")
        self.assertEqual(snapshot["asset_dir"], str((self.tmp / "assets").resolve()))
        self.assertEqual(config.lang_in, "en")
        self.assertEqual(config.lang_out, "ja")
        self.assertEqual(config.pages, "2-3")
        self.assertTrue(config.no_mono)
        self.assertFalse(config.no_dual)
        self.assertFalse(hasattr(config, "watermark_output_mode"))
        self.assertEqual(config.primary_font_family, "serif")
        self.assertFalse(config.add_formula_placehold_hint)

    def test_initialized_state_keeps_runtime_sources_single_owned(self) -> None:
        from file_task_pdf_translate.config import load_workspace_config
        from file_task_pdf_translate.state import load_or_init_state
        from file_task_pdf_translate.state import paths_for

        self.write_config(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".strip()
            + "\n",
        )

        snapshot = load_workspace_config(self.tmp).snapshot
        state = load_or_init_state(paths_for(self.tmp), snapshot)

        self.assertIn("config", state)
        self.assertEqual(state["config"]["config_hash"], snapshot["config_hash"])
        self.assertIsNone(state["pending_task_hash"])
        self.assertEqual(state["output_pdfs"], {})
        for field in (
            "input_pdf",
            "config_hash",
            "pending",
            "accepted",
            "output_pdf",
            "pipeline_progress",
            "pipeline_stages",
            "last_error",
        ):
            self.assertNotIn(field, state)

    def test_initialized_state_rejects_config_drift(self) -> None:
        from file_task_pdf_translate.config import load_workspace_config
        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import load_or_init_state
        from file_task_pdf_translate.state import paths_for

        self.write_config(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".strip()
            + "\n",
        )
        config = load_workspace_config(self.tmp)
        load_or_init_state(paths_for(self.tmp), config.snapshot)
        self.write_config(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: ja
asset_dir: assets
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".strip()
            + "\n",
        )

        result = advance(self.tmp)

        self.assertEqual(result["status"], "config_error")
        self.assertIn("changed", result["validation_errors"][0])

    def test_advance_reports_asset_error_before_initializing_state(self) -> None:
        from file_task_pdf_translate.runner import advance

        self.write_config(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: missing-assets
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".strip()
            + "\n",
        )

        result = advance(self.tmp)

        self.assertEqual(result["status"], "asset_error")
        self.assertIn("asset_dir", result["validation_errors"][0])
        self.assertFalse((self.tmp / ".pdf_translate" / "state.json").exists())


class RuntimeAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-assets-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_runtime_asset_dir_requires_manifest_and_hashes(self) -> None:
        from babeldoc.assets.assets import AssetError
        from babeldoc.assets.assets import set_runtime_asset_dir

        with self.assertRaises(AssetError) as ctx:
            set_runtime_asset_dir(self.tmp)

        self.assertIn("manifest", str(ctx.exception))

    def test_runtime_asset_dir_sets_tiktoken_cache_after_manifest_validation(self) -> None:
        from babeldoc.assets.assets import clear_runtime_asset_dir
        from babeldoc.assets.assets import set_runtime_asset_dir

        payload = b"asset"
        digest = hashlib.sha3_256(payload).hexdigest()
        (self.tmp / "models").mkdir()
        (self.tmp / "fonts").mkdir()
        (self.tmp / "cmap").mkdir()
        (self.tmp / "tiktoken").mkdir()
        (self.tmp / "models" / "model.onnx").write_bytes(payload)
        manifest = {
            "models": [{"name": "model.onnx", "sha3_256": digest}],
            "fonts": [],
            "cmap": [],
            "tiktoken": [],
        }
        (self.tmp / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        with patch(
            "babeldoc.assets.assets.generate_all_assets_file_list",
            return_value=manifest,
        ):
            asset_dir = set_runtime_asset_dir(self.tmp)
        self.addCleanup(clear_runtime_asset_dir)

        self.assertEqual(asset_dir, self.tmp.resolve())
        self.assertEqual(os.environ["TIKTOKEN_CACHE_DIR"], str(self.tmp / "tiktoken"))

    def test_download_runtime_assets_reuses_existing_valid_directory_offline(self) -> None:
        from babeldoc.assets.assets import download_runtime_assets

        payload = b"asset"
        digest = hashlib.sha3_256(payload).hexdigest()
        for group in ("models", "fonts", "cmap", "tiktoken"):
            (self.tmp / group).mkdir()
        (self.tmp / "models" / "model.onnx").write_bytes(payload)
        manifest = {
            "models": [{"name": "model.onnx", "sha3_256": digest}],
            "fonts": [],
            "cmap": [],
            "tiktoken": [],
        }
        (self.tmp / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        with patch(
            "babeldoc.assets.assets.generate_all_assets_file_list",
            return_value=manifest,
        ), patch(
            "babeldoc.assets.assets._download_doclayout_model_to",
            new=AsyncMock(side_effect=AssertionError("network path used")),
        ), patch(
            "babeldoc.assets.assets._download_fonts_to",
            new=AsyncMock(side_effect=AssertionError("network path used")),
        ), patch(
            "babeldoc.assets.assets._download_cmaps_to",
            new=AsyncMock(side_effect=AssertionError("network path used")),
        ):
            asset_dir = download_runtime_assets(self.tmp)

        self.assertEqual(asset_dir, self.tmp.resolve())


class DependencySpecificationRegressionTests(unittest.TestCase):
    def test_cryptography_requirement_respects_pyopenssl_runtime_bound(self) -> None:
        requirements = (
            REPO_ROOT / "pdf-translate" / "scripts" / "requirements.txt"
        ).read_text(encoding="utf-8").splitlines()

        self.assertIn("cryptography>=46.0.7,<47", requirements)

    def test_readme_distinguishes_python_dependencies_from_runtime_assets(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Python dependencies", readme)
        self.assertIn("runtime assets", readme)
        self.assertIn("requirements.txt installs Python packages", readme)
        self.assertIn("download_assets.py prepares runtime assets", readme)


class FontMappingRegressionTests(unittest.TestCase):
    def test_math_symbol_is_routed_to_symbol_font_pool(self) -> None:
        from babeldoc.format.pdf.document_il.utils.fontmap import _is_symbol_or_math

        self.assertTrue(_is_symbol_or_math("∼"))
        self.assertFalse(_is_symbol_or_math("A"))


class RenderCompareRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-render-compare-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_compare_renders_reports_numeric_page_numbers(self) -> None:
        import importlib.util

        script = REPO_ROOT / "pdf-translate" / "scripts" / "compare_renders.py"
        spec = importlib.util.spec_from_file_location("compare_renders", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = self.tmp / "source"
        target = self.tmp / "target"
        source.mkdir()
        target.mkdir()
        for page in range(1, 34):
            payload = b"same"
            if page == 3:
                (source / f"page-{page}.png").write_bytes(payload)
                (target / f"page-{page}.png").write_bytes(b"changed")
            else:
                (source / f"page-{page}.png").write_bytes(payload)
                (target / f"page-{page}.png").write_bytes(payload)

        result = module.compare_renders(source, target)

        self.assertEqual(result["changed_pages"], [3])


class OutputSelectionTests(unittest.TestCase):
    def test_output_pdfs_exposes_every_generated_variant_and_primary_path(self) -> None:
        from file_task_pdf_translate.runner import _collect_output_pdfs

        result = SimpleNamespace(
            mono_pdf_path=Path("out/clean.mono.pdf"),
            dual_pdf_path=Path("out/clean.dual.pdf"),
        )
        output_pdfs, primary = _collect_output_pdfs(
            result,
            {
                "output_mode": "dual",
            },
        )

        self.assertNotIn("mono", output_pdfs)
        self.assertEqual(output_pdfs["dual"], "out\\clean.dual.pdf")
        self.assertEqual(primary, "out\\clean.dual.pdf")

    def test_output_pdf_validation_rejects_visible_internal_markers(self) -> None:
        import pymupdf

        from file_task_pdf_translate.runner import _validate_output_pdfs

        with tempfile.TemporaryDirectory(prefix="pdf-translate-output-leak-test-") as tmp:
            pdf_path = Path(tmp) / "leak.pdf"
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=120)
            page.insert_text((30, 60), "visible <b1> and {{FORMULA_1}}")
            doc.save(pdf_path)
            doc.close()

            errors = _validate_output_pdfs({"mono": str(pdf_path)})

        self.assertTrue(errors)
        self.assertIn("leaks internal markers", errors[0])


class ToUnicodeCMapRegressionTests(unittest.TestCase):
    def test_cmap_rewrite_only_targets_pipeline_owned_identity_fonts(self) -> None:
        from babeldoc.format.pdf.document_il.backend.pdf_creater import (
            _is_pipeline_owned_identity_font,
        )

        self.assertTrue(
            _is_pipeline_owned_identity_font(
                (
                    2047,
                    "ttf",
                    "Type0",
                    "DNBALE+Source Han Serif CN Regular",
                    "SourceHanSerifCN-Regular.ttf",
                    "Identity-H",
                )
            )
        )
        self.assertFalse(
            _is_pipeline_owned_identity_font(
                (
                    42,
                    "ttf",
                    "Type0",
                    "AAAAAA+SourceOwnedFont",
                    "SourceOwnedFont.ttf",
                    "Identity-H",
                )
            )
        )

    def test_rebuilt_tounicode_uses_utf16be_and_normalizes_cjk_compatibility(self) -> None:
        from babeldoc.format.pdf.document_il.backend.pdf_creater import make_tounicode
        from babeldoc.format.pdf.document_il.backend.pdf_creater import (
            parse_tounicode_cmap,
        )

        cmap = parse_tounicode_cmap(
            b"""
1 beginbfchar
<0001> <1f10b>
<0002> <f97e>
endbfchar
"""
        )

        rebuilt = make_tounicode(cmap, [1, 2])

        self.assertIn("<0001><d83cdd0b>", rebuilt)
        self.assertIn("<0002><91cf>", rebuilt)
        self.assertNotIn("<1f10b>", rebuilt)
        self.assertNotIn("<f97e>", rebuilt)


class PagePlanRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-page-plan-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_page_plan_tracks_target_active_and_completed_pages(self) -> None:
        from file_task_pdf_translate.state import ensure_page_plan
        from file_task_pdf_translate.state import mark_active_page_completed

        state = {"config": {"pages": None}}

        changed = ensure_page_plan(state, 3)

        self.assertTrue(changed)
        self.assertEqual(state["page_plan"]["target_pages"], [1, 2, 3])
        self.assertEqual(state["page_plan"]["active_page"], 1)
        self.assertEqual(state["page_plan"]["completed_pages"], [])

        completed, next_page = mark_active_page_completed(state)

        self.assertEqual(completed, 1)
        self.assertEqual(next_page, 2)
        self.assertEqual(state["page_plan"]["active_page"], 2)
        self.assertEqual(state["page_plan"]["completed_pages"], [1])

    def test_single_page_shard_config_uses_active_page_and_private_output_dir(self) -> None:
        from file_task_pdf_translate.runner import _build_translation_config
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.translator import FileTaskTranslator

        source_pdf = self.tmp / "paper.pdf"
        write_test_pdf(source_pdf, ["page 1", "page 2", "page 3"])
        paths = paths_for(self.tmp)
        state = {
            "config": {
                "input_pdf": str(source_pdf),
                "lang_in": "en",
                "lang_out": "zh-CN",
                "pages": None,
                "output_mode": "mono",
                "primary_font_family": None,
                "add_formula_placehold_hint": True,
            },
            "page_plan": {
                "source_page_count": 3,
                "target_pages": [1, 2, 3],
                "active_page": 2,
                "completed_pages": [1],
            },
        }
        translator = FileTaskTranslator(paths, state)

        with patch(
            "babeldoc.docvision.doclayout.OnnxModel.load_available",
            return_value=object(),
        ):
            config = _build_translation_config(paths, state, translator)

        self.assertEqual(config.pages, "2")
        self.assertEqual(config.page_ranges, [(2, 2)])
        self.assertEqual(Path(config.output_dir), paths.page_outputs / "page_0002")
        self.assertEqual(
            Path(config.working_dir),
            paths.working / "page_0002" / "paper",
        )

    def test_page_output_merge_replaces_only_active_page(self) -> None:
        import pymupdf

        from file_task_pdf_translate.runner import _merge_page_output_pdfs
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source_pdf = self.tmp / "paper.pdf"
        shard_pdf = self.tmp / "shard.pdf"
        write_test_pdf(source_pdf, ["original page one", "original page two", "original page three"])
        write_test_pdf(shard_pdf, ["wrong page one", "translated page two", "wrong page three"])
        state = {
            "config": {
                "input_pdf": str(source_pdf),
                "output_mode": "mono",
            },
            "page_plan": {
                "source_page_count": 3,
                "target_pages": [2],
                "active_page": 2,
                "completed_pages": [],
            },
            "output_pdfs": {},
        }

        output_pdfs, primary = _merge_page_output_pdfs(
            paths,
            state,
            {"mono": str(shard_pdf)},
        )

        self.assertEqual(primary, output_pdfs["mono"])
        merged = pymupdf.open(primary)
        try:
            page_texts = [page.get_text("text") for page in merged]
        finally:
            merged.close()
        self.assertIn("original page one", page_texts[0])
        self.assertIn("translated page two", page_texts[1])
        self.assertIn("original page three", page_texts[2])
        self.assertNotIn("wrong page one", page_texts[0])
        self.assertNotIn("wrong page three", page_texts[2])

    def test_advance_returns_page_completed_before_next_page(self) -> None:
        import pymupdf

        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json

        workspace = self.tmp
        write_test_pdf(workspace / "paper.pdf", ["source page one", "source page two"])
        (workspace / "assets").mkdir()
        (workspace / "pdf_translate.yaml").write_text(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
pages: "1-2"
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".lstrip(),
            encoding="utf-8",
        )
        output_pdf = workspace / "translated.pdf"
        write_test_pdf(output_pdf, ["translated page one", "unused page two"])
        translate_result = SimpleNamespace(
            mono_pdf_path=output_pdf,
            dual_pdf_path=None,
        )

        with patch(
            "file_task_pdf_translate.runner.set_runtime_asset_dir",
            return_value=workspace / "assets",
        ), patch(
            "file_task_pdf_translate.runner.translate",
            return_value=translate_result,
        ), patch(
            "file_task_pdf_translate.runner.shutdown_file_task_runtime",
        ):
            result = advance(workspace)

        state = read_json(paths_for(workspace).state, {})
        merged = pymupdf.open(result["output_pdf"])
        try:
            page_texts = [page.get_text("text") for page in merged]
        finally:
            merged.close()

        self.assertEqual(result["status"], "page_completed")
        self.assertEqual(result["completed_page"], 1)
        self.assertEqual(result["next_page"], 2)
        self.assertEqual(state["status"], "page_completed")
        self.assertEqual(state["page_plan"]["completed_pages"], [1])
        self.assertEqual(state["page_plan"]["active_page"], 2)
        self.assertIn("translated page one", page_texts[0])
        self.assertIn("source page two", page_texts[1])

    def test_advance_finalizes_ready_shard_without_rerunning_translation(self) -> None:
        import pymupdf

        from file_task_pdf_translate.config import load_workspace_config
        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import default_state
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        workspace = self.tmp
        write_test_pdf(workspace / "paper.pdf", ["source page one", "source page two"])
        (workspace / "assets").mkdir()
        (workspace / "pdf_translate.yaml").write_text(
            """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
pages: "1-2"
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".lstrip(),
            encoding="utf-8",
        )
        shard_pdf = workspace / "translated.pdf"
        write_test_pdf(shard_pdf, ["translated page one", "unused page two"])
        paths = paths_for(workspace)
        ensure_dirs(paths)
        state = default_state(load_workspace_config(workspace).snapshot)
        state["status"] = "shard_ready"
        state["page_plan"] = {
            "source_page_count": 2,
            "target_pages": [1, 2],
            "active_page": 1,
            "completed_pages": [],
        }
        state["shard_ready"] = {
            "page": 1,
            "output_pdfs": {"mono": str(shard_pdf)},
        }
        write_json(paths.state, state)

        with patch(
            "file_task_pdf_translate.runner.set_runtime_asset_dir",
            return_value=workspace / "assets",
        ), patch(
            "file_task_pdf_translate.runner.translate",
            side_effect=AssertionError("translate reran"),
        ):
            result = advance(workspace)

        state = read_json(paths.state, {})
        merged = pymupdf.open(result["output_pdf"])
        try:
            page_texts = [page.get_text("text") for page in merged]
        finally:
            merged.close()

        self.assertEqual(result["status"], "page_completed")
        self.assertNotIn("shard_ready", state)
        self.assertEqual(state["page_plan"]["completed_pages"], [1])
        self.assertIn("translated page one", page_texts[0])
        self.assertIn("source page two", page_texts[1])

    def test_progress_snapshot_reports_business_page_cursor(self) -> None:
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_json(
            paths.state,
            {
                "status": "running",
                "page_plan": {
                    "source_page_count": 3,
                    "target_pages": [1, 2, 3],
                    "active_page": 2,
                    "completed_pages": [1],
                },
            },
        )

        _record_pipeline_progress(
            paths,
            {
                "type": "progress_update",
                "stage": "Parse Page Layout",
                "stage_progress": 50.0,
                "stage_current": 1,
                "stage_total": 1,
                "overall_progress": 25.0,
                "part_index": 1,
                "total_parts": 1,
            },
        )

        progress = read_json(paths.progress, None)
        self.assertEqual(progress["page_plan"]["active_page"], 2)
        self.assertEqual(progress["page_progress"]["completed_count"], 1)
        self.assertEqual(progress["page_progress"]["overall_progress"], 33.33)
        self.assertEqual(progress["stage_total"], 1)
        self.assertEqual(progress["shard_stage_total"], 1)
        self.assertEqual(progress["workflow_progress"], 33.33)

    def test_progress_response_does_not_reannotate_page_snapshot(self) -> None:
        from file_task_pdf_translate.runner import _progress
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        state = {
            "status": "needs_ai_edit",
            "page_plan": {
                "source_page_count": 3,
                "target_pages": [1, 2, 3],
                "active_page": 2,
                "completed_pages": [1],
            },
        }
        write_json(paths.state, state)
        _record_pipeline_progress(
            paths,
            {
                "type": "progress_paused",
                "stage": "Translate Paragraphs",
                "stage_progress": 50.0,
                "stage_current": 1,
                "stage_total": 2,
                "overall_progress": 50.0,
                "part_index": 1,
                "total_parts": 1,
            },
        )

        progress = _progress(paths, state)

        self.assertEqual(progress["page_progress"]["active_page_progress"], 0.0)
        self.assertEqual(progress["page_progress"]["overall_progress"], 33.33)
        self.assertEqual(
            progress["pipeline_progress"]["page_progress"]["active_page_progress"],
            0.0,
        )
        self.assertEqual(progress["pipeline_progress"]["shard_stage_total"], 2)
        self.assertEqual(progress["pipeline_progress"]["stage_total"], 2)
        self.assertEqual(progress["pipeline_progress"]["workflow_progress"], 33.33)

    def test_single_target_page_progress_reports_one_page_total(self) -> None:
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_json(
            paths.state,
            {
                "status": "needs_ai_edit",
                "page_plan": {
                    "source_page_count": 33,
                    "target_pages": [1],
                    "active_page": 1,
                    "completed_pages": [],
                },
            },
        )

        _record_pipeline_progress(
            paths,
            {
                "type": "progress_paused",
                "stage": "Translate Paragraphs",
                "stage_progress": 39.0,
                "stage_current": 13,
                "stage_total": 33,
                "overall_progress": 39.0,
                "part_index": 1,
                "total_parts": 1,
            },
        )

        progress = read_json(paths.progress, None)
        self.assertEqual(progress["stage_total"], 33)
        self.assertEqual(progress["shard_stage_total"], 33)
        self.assertEqual(progress["page_progress"]["target_total"], 1)
        self.assertEqual(progress["workflow_progress"], 0.0)

    def test_pending_progress_uses_accepted_over_current_task(self) -> None:
        from file_task_pdf_translate.runner import _progress
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import answer_path
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import pending_snapshot_path
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        state = {
            "status": "needs_ai_edit",
            "pending_task_hash": "pending",
            "page_plan": {
                "source_page_count": 33,
                "target_pages": [1],
                "active_page": 1,
                "completed_pages": [],
            },
        }
        write_json(paths.state, state)
        write_json(
            pending_snapshot_path(paths, "pending"),
            {"task_type": "translate", "blocks": [{"source": "x"}], "page": 1},
        )
        write_json(answer_path(paths, "accepted"), [{"id": 0, "output": "x"}])
        _record_pipeline_progress(
            paths,
            {
                "type": "progress_paused",
                "stage": "Translate Paragraphs",
                "stage_progress": 100.0,
                "stage_current": 33,
                "stage_total": 33,
                "overall_progress": 100.0,
                "part_index": 1,
                "total_parts": 1,
            },
        )

        progress = _progress(paths, state)

        self.assertEqual(progress["pipeline_progress"]["stage_progress"], 100.0)
        self.assertEqual(progress["pipeline_progress"]["workflow_progress"], 0.0)
        self.assertEqual(progress["page_progress"]["workflow_current"], 0)
        self.assertEqual(progress["page_progress"]["workflow_total"], 1)


class EditableParserRegressionTests(unittest.TestCase):
    def test_empty_multi_item_translation_yaml_remains_distinct_items(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import parse_editable_document
        from file_task_pdf_translate.editable import render_editable_document

        text = render_editable_document(
            "translate",
            [
                EditableBlock(source="alpha"),
                EditableBlock(source="beta"),
            ],
            "zh-CN",
        )

        document, errors = parse_editable_document(text)

        self.assertEqual(errors, [])
        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.task, "translate")
        self.assertEqual([block.source for block in document.items], ["alpha", "beta"])
        self.assertEqual([block.translation for block in document.items], ["", ""])
        self.assertNotIn("[[", text)
        self.assertNotIn("⟦", text)

    def test_rendered_editable_items_can_include_readonly_context(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import parse_editable_document
        from file_task_pdf_translate.editable import render_editable_document

        text = render_editable_document(
            "translate",
            [
                EditableBlock(
                    source="els [5]",
                    context_before="fast T2I models",
                    text_role="protected",
                )
            ],
            "zh-CN",
        )

        self.assertIn("context_before: fast T2I models", text)
        self.assertIn("text_role: protected", text)
        document, errors = parse_editable_document(text)
        self.assertEqual(errors, [])
        self.assertIsNotNone(document)

    def test_term_extract_task_is_rejected(self) -> None:
        from file_task_pdf_translate.editable import parse_editable_document

        document, errors = parse_editable_document(
            """
task: term_extract
target_language: zh-CN
items:
  - id: 1
    source: weather labels
    translation: ""
""".lstrip()
        )

        self.assertIsNone(document)
        self.assertEqual(errors, ["task must be translate"])

    def test_protected_tokens_are_plain_text_yaml_values(self) -> None:
        from file_task_pdf_translate.editable import placeholder_sequence

        display = "Use <b0> and <b1>."

        self.assertEqual(placeholder_sequence(display), ["<b0>", "<b1>"])
        self.assertNotIn("[[", display)
        self.assertNotIn("⟦", display)

    def test_protected_tokens_are_detected_when_adjacent_to_text_and_digits(self) -> None:
        from file_task_pdf_translate.editable import placeholder_sequence

        display = "<b6></b7>3<b8>"

        self.assertEqual(
            placeholder_sequence(display),
            ["<b6>", "</b7>", "<b8>"],
        )
        self.assertEqual(
            placeholder_sequence("Liangsi Lu<b1>Xuhang Chen"),
            ["<b1>"],
        )
        self.assertEqual(
            placeholder_sequence("<b1><b2>Corresponding author"),
            ["<b1>", "<b2>"],
        )
        self.assertEqual(
            placeholder_sequence(" <b1>ChordEdit</b1>,"),
            ["<b1>", "</b1>"],
        )

    def test_extracted_pdf_text_is_normalized_before_editable_tasks(self) -> None:
        from file_task_pdf_translate.editable import normalize_extracted_pdf_text
        from file_task_pdf_translate.text_hygiene import HygieneBlock
        from file_task_pdf_translate.text_hygiene import normalize_text_blocks

        source = (
            "The (cid:82) sta- bility proof uses measurementsR as a "
            "thesemodelsoffer unprecedentedspeed, promisingtruly "
            "introduceChordEdit and lowenergy control. dog -<b1>lion."
        )

        normalized = normalize_extracted_pdf_text(source)

        self.assertIn("†", normalized)
        self.assertIn("stability", normalized)
        self.assertIn("measurements R", normalized)
        self.assertIn("these models offer", normalized)
        self.assertIn("unprecedented speed", normalized)
        self.assertIn("promising truly", normalized)
        self.assertIn("introduce ChordEdit", normalized)
        self.assertIn("low energy", normalized)
        self.assertIn("dog -> lion", normalized)
        self.assertNotIn("<b1>", normalized)
        blocks = normalize_text_blocks(
            [
                HygieneBlock("method that fa-"),
                HygieneBlock("cilitates high-fidelity one-step editing."),
            ]
        )
        self.assertEqual(
            [block.text for block in blocks],
            ["method that facilitates high-fidelity one-step editing."],
        )

    def test_author_affiliation_boundaries_are_structurally_separated(self) -> None:
        from file_task_pdf_translate.editable import normalize_extracted_pdf_text

        source = (
            "Minzhe Guo1,Shichu Li3,Jingchao Wang4,Yang Shi1†"
            "1Guangdong University of Technology2Huizhou University"
            "3Shenzhen University4Peking University"
        )

        normalized = normalize_extracted_pdf_text(source)

        self.assertIn("Guo 1, Shichu", normalized)
        self.assertIn("Li 3, Jingchao", normalized)
        self.assertIn("Wang 4, Yang", normalized)
        self.assertIn("1 Guangdong University", normalized)
        self.assertIn("Technology 2 Huizhou University", normalized)
        self.assertIn("University 3 Shenzhen University", normalized)
        self.assertIn("University 4 Peking University", normalized)

    def test_geometry_context_restores_author_word_boundaries(self) -> None:
        from file_task_pdf_translate.editable import normalize_extracted_pdf_text

        chars = []
        x = 0.0
        previous = ""
        for char in "Lu1 , MinzheGuo Universityof":
            width = 3.0 if char == " " else 5.0
            if (previous, char) in {("e", "G"), ("y", "o")}:
                x += 3.0
            chars.append({"text": char, "bbox": [x, 0.0, x + width, 8.0]})
            x += width
            previous = char

        normalized = normalize_extracted_pdf_text(
            "Lu1 , MinzheGuo Universityof",
            {
                "layout_label": "author",
                "chars": chars,
            },
        )

        self.assertIn("Lu 1, Minzhe", normalized)
        self.assertIn("Minzhe Guo", normalized)
        self.assertIn("University of", normalized)

    def test_geometry_context_repairs_line_break_hyphenation(self) -> None:
        from file_task_pdf_translate.editable import normalize_extracted_pdf_text

        def chars_for(text: str) -> list[dict]:
            chars = []
            x = 0.0
            y = 0.0
            for char in text:
                if char == "\n":
                    x = 0.0
                    y += 12.0
                    continue
                chars.append({"text": char, "bbox": [x, y, x + 5.0, y + 8.0]})
                x += 5.0
            return chars

        normalized = normalize_extracted_pdf_text(
            "re-\nsulting text-\nguided text-to-\nimage Training-\nfree PIE-\nbench T-\nfree",
            {
                "chars": chars_for(
                    "re-\nsulting text-\nguided text-to-\nimage Training-\nfree PIE-\nbench T-\nfree"
                ),
            },
        )

        self.assertIn("resulting", normalized)
        self.assertIn("text-guided", normalized)
        self.assertIn("text-to-image", normalized)
        self.assertIn("Training-free", normalized)
        self.assertIn("PIE-bench", normalized)
        self.assertIn("T-free", normalized)

    def test_geometry_context_repairs_citation_spacing(self) -> None:
        from file_task_pdf_translate.editable import normalize_extracted_pdf_text

        normalized = normalize_extracted_pdf_text(
            "inversion [81 1, 15] and PIEbench [1 1]",
            {"chars": [{"text": char, "bbox": [i * 5.0, 0.0, i * 5.0 + 4.0, 8.0]} for i, char in enumerate("inversion [81 1, 15] and PIEbench [1 1]")]},
        )

        self.assertIn("[8-11, 15]", normalized)
        self.assertIn("PIE-bench [11]", normalized)

    def test_placeholder_author_boundaries_are_spaced_before_editable_tasks(self) -> None:
        from file_task_pdf_translate.editable import normalize_extracted_pdf_text

        source = (
            "Liangsi Lu<b1>Xuhang Chen<b2>Minzhe Guo<b3>Shichu Li"
            "<b4>Jingchao Wang<b5>Yang Shi<b6><b7>Guangdong University "
            "of Technology <b8>Huizhou University<b9>Shenzhen University"
        )

        normalized = normalize_extracted_pdf_text(
            source,
            {"layout_label": "author"},
        )

        self.assertIn("Lu <b1>Xuhang", normalized)
        self.assertIn("Chen <b2>Minzhe", normalized)
        self.assertIn("Guo <b3>Shichu", normalized)
        self.assertIn("University <b9>Shenzhen", normalized)

    def test_short_figure_labels_are_detected_for_term_split(self) -> None:
        from file_task_pdf_translate.text_hygiene import classify_text_role
        from file_task_pdf_translate.text_hygiene import is_figure_label_candidate

        self.assertTrue(is_figure_label_candidate("snow", "plain text"))
        self.assertTrue(is_figure_label_candidate("Edited image", "figure"))
        self.assertEqual(classify_text_role("Edited image", "caption"), "caption")
        self.assertTrue(
            is_figure_label_candidate(
                "snow",
                "fallback_line",
                {
                    "bbox": [170.0, 470.0, 191.0, 476.0],
                    "page_layouts": [
                        {
                            "class_name": "figure",
                            "bbox": [56.0, 281.0, 555.0, 648.0],
                        }
                    ],
                },
            )
        )
        self.assertEqual(
            classify_text_role(
                "Figure 1. ChordEdit mitigates failures.",
                "figure_caption",
            ),
            "caption",
        )
        self.assertEqual(classify_text_role("?", "plain text"), "table_marker")
        self.assertEqual(
            classify_text_role("Direct Inversion + MasaCtrl [3, 1 1]", "plain text"),
            "protected",
        )
        self.assertFalse(is_figure_label_candidate("ChordEdit"))
        self.assertFalse(
            is_figure_label_candidate(
                "Figure 1. ChordEdit mitigates the failures of naive editing.",
                "caption",
            )
        )

    def test_paragraph_finder_merges_hyphenated_continuation_paragraphs(self) -> None:
        from babeldoc.format.pdf.document_il.il_version_1 import Box
        from babeldoc.format.pdf.document_il.il_version_1 import PdfCharacter
        from babeldoc.format.pdf.document_il.il_version_1 import PdfLine
        from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraph
        from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraphComposition
        from babeldoc.format.pdf.document_il.il_version_1 import PdfStyle
        from babeldoc.format.pdf.document_il.il_version_1 import VisualBbox
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder

        def paragraph(text: str, y: float) -> PdfParagraph:
            style = PdfStyle(font_id="f1", font_size=10)
            chars = []
            for index, char in enumerate(text):
                box = Box(index * 5, y, index * 5 + 4, y + 8)
                chars.append(
                    PdfCharacter(
                        pdf_style=style,
                        box=box,
                        visual_bbox=VisualBbox(box=box),
                        char_unicode=char,
                        xobj_id=7,
                    )
                )
            line = PdfLine(
                box=Box(0, y, max(len(text), 1) * 5, y + 8),
                pdf_character=chars,
            )
            return PdfParagraph(
                box=line.box,
                pdf_style=style,
                pdf_paragraph_composition=[PdfParagraphComposition(pdf_line=line)],
                xobj_id=7,
                unicode=text,
                layout_id=42,
            )

        paragraphs = [paragraph("fa-", 10), paragraph("cilitates", 24)]

        finder = object.__new__(ParagraphFinder)
        finder.merge_hyphenated_continuation_paragraphs(paragraphs)

        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].unicode, "facilitates")

class EditableYamlWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-yaml-workflow-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_translation_pending_writes_yaml_and_accepts_yaml_answer(self) -> None:
        from babeldoc.file_task_bridge import FileTaskPending
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import parse_editable_document
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.translator import FileTaskTranslator
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        state = {
            "config": {
                "lang_in": "en",
                "lang_out": "zh-CN",
            },
            "pending_task_hash": None,
            "status": "initialized",
        }
        write_json(paths.state, state)
        translator = FileTaskTranslator(paths, state)

        with self.assertRaises(FileTaskPending):
            translator.do_translate("hello <b1>world</b1>")

        self.assertEqual(paths.current_translation.name, "current_translation.yaml")
        self.assertTrue(paths.current_translation.exists())
        self.assertFalse((self.tmp / "current_translation.txt").exists())
        document, errors = parse_editable_document(
            paths.current_translation.read_text(encoding="utf-8")
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(document.task, "translate")
        self.assertEqual(document.items[0].source, "hello <b1>world</b1>")

        state = read_json(paths.state, None)
        paths.current_translation.write_text(
            render_editable_document(
                "translate",
                [
                    EditableBlock(
                        source="hello <b1>world</b1>",
                        translation="你好 <b1>世界</b1>",
                    )
                ],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        task_hash = state["pending_task_hash"]
        result = validate_pending(paths, state)

        self.assertTrue(result.accepted)
        accepted = read_json(
            paths.accepted / f"{task_hash}.answer.json",
            None,
        )
        self.assertEqual(accepted, [{"id": 0, "output": "你好 <b1>世界</b1>"}])
        final_state = read_json(paths.state, None)
        self.assertIsNone(final_state["pending_task_hash"])
        self.assertNotIn("accepted", final_state)

    def test_translation_task_hash_ignores_random_hygiene_context(self) -> None:
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.translator import FileTaskTranslator

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        state = {
            "config": {
                "lang_in": "en",
                "lang_out": "zh-CN",
            },
            "pending_task_hash": None,
            "status": "running",
        }
        translator = FileTaskTranslator(paths, state)

        first = translator._translation_task_from_items(
            [
                {
                    "id": 0,
                    "input": "hello",
                    "hygiene_context": {"debug_id": "random-a"},
                }
            ]
        )
        second = translator._translation_task_from_items(
            [
                {
                    "id": 0,
                    "input": "hello",
                    "hygiene_context": {"debug_id": "random-b"},
                }
            ]
        )

        self.assertEqual(first["task_hash"], second["task_hash"])

    def test_translation_task_keeps_previous_page_context_for_continuation(self) -> None:
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.translator import FileTaskTranslator

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source_pdf = self.tmp / "paper.pdf"
        write_test_pdf(source_pdf, ["fast T2I models", "els [5] continue"])
        state = {
            "config": {
                "lang_in": "en",
                "lang_out": "zh-CN",
                "input_pdf": str(source_pdf),
            },
            "page_plan": {
                "active_page": 2,
            },
            "pending_task_hash": None,
            "status": "running",
        }
        translator = FileTaskTranslator(paths, state)

        task = translator._translation_task_from_items([{"id": 0, "input": "els [5]"}])

        self.assertIn("fast T2I models", task["blocks"][0]["context_before"])

    def test_translation_task_repairs_hyphenated_item_boundary(self) -> None:
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.translator import FileTaskTranslator

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        state = {
            "config": {
                "lang_in": "en",
                "lang_out": "zh-CN",
            },
            "pending_task_hash": None,
            "status": "running",
        }
        translator = FileTaskTranslator(paths, state)

        task = translator._translation_task_from_items(
            [
                {"id": 0, "input": "method that fa-"},
                {"id": 1, "input": "cilitates high-fidelity editing."},
            ]
        )

        self.assertEqual(task["blocks"][0]["source"], "method that facilitates")
        self.assertEqual(task["blocks"][1]["source"], "high-fidelity editing.")

    def test_translation_validation_reports_placeholder_sequence_diff(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import pending_snapshot_path
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source = "<b1>Eq.</b1><b2>4.5</b2>"
        pending = {
            "task_type": "translate",
            "task_hash": "placeholder-mismatch",
            "blocks": [
                {
                    "source": source,
                    "original_source": source,
                    "required_placeholders": ["<b1>", "</b1>", "<b2>", "</b2>"],
                }
            ],
        }
        state = {
            "pending_task_hash": pending["task_hash"],
            "status": "needs_ai_edit",
        }
        write_json(pending_snapshot_path(paths, pending["task_hash"]), pending)
        write_json(paths.state, state)
        paths.current_translation.write_text(
            render_editable_document(
                "translate",
                [
                    EditableBlock(
                        source=source,
                        translation="<b2>Eq.</b1><b1>4.5</b2>",
                    )
                ],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        result = validate_pending(paths, state)

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "needs_ai_fix")
        message = "\n".join(result.errors)
        self.assertIn("mismatch at marker 1", message)
        self.assertIn("order mismatch", message)
        self.assertIn("expected <b1>", message)
        self.assertIn("actual <b2>", message)
        self.assertIn("expected window", message)
        self.assertIn("actual window", message)

    def test_translation_validation_preserves_source_placeholders_in_answer(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import pending_snapshot_path
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source = "<b1> text"
        pending = {
            "task_type": "translate",
            "task_hash": "preserve-source-placeholder",
            "blocks": [
                {
                    "source": source,
                    "original_source": "<b1> text",
                    "required_placeholders": ["<b1>"],
                }
            ],
        }
        state = {
            "pending_task_hash": pending["task_hash"],
            "status": "needs_ai_edit",
        }
        write_json(pending_snapshot_path(paths, pending["task_hash"]), pending)
        write_json(paths.state, state)
        paths.current_translation.write_text(
            render_editable_document(
                "translate",
                [EditableBlock(source=source, translation=source)],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        result = validate_pending(paths, state)

        self.assertTrue(result.accepted)
        accepted = read_json(paths.accepted / "preserve-source-placeholder.answer.json", None)
        self.assertEqual(accepted, [{"id": 0, "output": "<b1> text"}])

    def test_translation_validation_preserves_real_chordedit_marker(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import pending_snapshot_path
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source = "<b1>ChordEdit</b1> mitigates editing failures."
        pending = {
            "task_type": "translate",
            "task_hash": "preserve-chordedit-marker",
            "blocks": [
                {
                    "source": source,
                    "original_source": source,
                    "required_placeholders": ["<b1>", "</b1>"],
                }
            ],
        }
        state = {
            "pending_task_hash": pending["task_hash"],
            "status": "needs_ai_edit",
        }
        write_json(pending_snapshot_path(paths, pending["task_hash"]), pending)
        write_json(paths.state, state)
        paths.current_translation.write_text(
            render_editable_document(
                "translate",
                [
                    EditableBlock(
                        source=source,
                        translation="<b1>ChordEdit</b1> 缓解了编辑失败。",
                    )
                ],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        result = validate_pending(paths, state)

        self.assertTrue(result.accepted)
        accepted = read_json(paths.accepted / "preserve-chordedit-marker.answer.json", None)
        self.assertEqual(
            accepted,
            [{"id": 0, "output": "<b1>ChordEdit</b1> 缓解了编辑失败。"}],
        )

    def test_translation_validation_rejects_added_placeholders(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import pending_snapshot_path
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        pending = {
            "task_type": "translate",
            "task_hash": "raw-internal-marker",
            "blocks": [
                {
                    "source": "plain source",
                    "original_source": "plain source",
                    "required_placeholders": [],
                }
            ],
        }
        state = {
            "pending_task_hash": pending["task_hash"],
            "status": "needs_ai_edit",
        }
        write_json(pending_snapshot_path(paths, pending["task_hash"]), pending)
        write_json(paths.state, state)
        paths.current_translation.write_text(
            render_editable_document(
                "translate",
                [EditableBlock(source="plain source", translation="plain <b1>")],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        result = validate_pending(paths, state)

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "needs_ai_fix")
        self.assertIn("extra", "\n".join(result.errors))


class TranslationSelectionRegressionTests(unittest.TestCase):
    def test_file_task_translation_keeps_short_visible_labels(self) -> None:
        from babeldoc.format.pdf.document_il.midend import il_translator_llm_only

        translator = object.__new__(il_translator_llm_only.ILTranslatorLLMOnly)
        translator.translation_config = SimpleNamespace(
            min_text_length=5,
            file_task_workflow=True,
        )
        paragraph = SimpleNamespace(
            debug_id="p1",
            unicode="fox",
            pdf_paragraph_composition=[],
            layout_label="plain text",
        )

        with patch.object(
            il_translator_llm_only,
            "is_cid_paragraph",
            return_value=False,
        ), patch.object(
            il_translator_llm_only,
            "is_placeholder_only_paragraph",
            return_value=False,
        ):
            self.assertFalse(translator._is_below_translation_length(paragraph))
            self.assertTrue(translator._should_translate_paragraph(paragraph))

    def test_file_task_translation_submits_one_batch_for_whole_page(self) -> None:
        from babeldoc.format.pdf.document_il.midend import il_translator_llm_only

        translator = object.__new__(il_translator_llm_only.ILTranslatorLLMOnly)
        shared_context = SimpleNamespace(
            first_paragraph=None,
            recent_title_paragraph=None,
        )
        translator.translation_config = SimpleNamespace(
            file_task_workflow=True,
            raise_if_cancelled=lambda: None,
            shared_context_cross_split_part=shared_context,
        )
        translator.calc_token_count = lambda text: 50
        translator.mid = 0
        paragraphs = [
            SimpleNamespace(
                debug_id=f"p{index}",
                unicode=f"paragraph {index}",
                layout_label="plain text",
                pdf_paragraph_composition=[],
            )
            for index in range(8)
        ]
        page = SimpleNamespace(pdf_font=[], pdf_xobject=[], pdf_paragraph=paragraphs)
        executor = SimpleNamespace(submit=Mock())
        pbar = SimpleNamespace(advance=Mock())

        with patch.object(
            il_translator_llm_only,
            "is_cid_paragraph",
            return_value=False,
        ), patch.object(
            il_translator_llm_only,
            "is_placeholder_only_paragraph",
            return_value=False,
        ):
            translator.process_page(
                page,
                executor,
                pbar,
                SimpleNamespace(new_paragraph=lambda: object()),
                SimpleNamespace(),
                set(),
            )

        executor.submit.assert_called_once()
        batch = executor.submit.call_args.args[1]
        self.assertEqual(batch.paragraphs, paragraphs)
        self.assertEqual(batch.pages, [page] * len(paragraphs))

    def test_translation_stage_list_has_no_term_extraction(self) -> None:
        from babeldoc.format.pdf.high_level import get_translation_stage

        stages = get_translation_stage(
            SimpleNamespace(
                only_parse_generate_pdf=False,
                skip_scanned_detection=False,
                skip_translation=False,
            )
        )
        stage_names = [name for name, _weight in stages]

        self.assertIn("Translate Paragraphs", stage_names)
        self.assertFalse(any("Term" in name for name in stage_names))


class PdfCompatibilityRegressionTests(unittest.TestCase):
    def test_type3_charproc_metrics_move_before_leading_save(self) -> None:
        from babeldoc.format.pdf.babelpdf.type3 import (
            _normalize_type3_charproc_stream,
        )

        normalized = _normalize_type3_charproc_stream(
            b"q\n0 0 500 700 0 0 d1\n0 0 m 10 10 l S\nQ\n"
        )

        self.assertEqual(
            normalized,
            b"0 0 500 700 0 0 d1\nq\n0 0 m 10 10 l S\nQ\n",
        )

    def test_fix_null_xref_preserves_page_annotations(self) -> None:
        import pymupdf

        from babeldoc.format.pdf.high_level import fix_null_xref

        with tempfile.TemporaryDirectory(prefix="pdf-translate-annot-test-") as tmp:
            pdf_path = Path(tmp) / "annotated.pdf"
            doc = pymupdf.open()
            page = doc.new_page(width=200, height=200)
            page.add_text_annot(pymupdf.Point(40, 40), "note")
            doc.save(pdf_path)
            doc.close()

            reopened = pymupdf.open(pdf_path)
            try:
                self.assertEqual(len(list(reopened[0].annots() or [])), 1)

                fix_null_xref(reopened)

                self.assertEqual(len(list(reopened[0].annots() or [])), 1)
            finally:
                reopened.close()

    def test_curve_render_unit_accepts_string_numeric_ctm_and_path_values(self) -> None:
        from bitstring import BitStream

        from babeldoc.format.pdf.document_il import il_version_1
        from babeldoc.format.pdf.document_il.backend.pdf_creater import CurveRenderUnit

        curve = il_version_1.PdfCurve(
            box=il_version_1.Box(x=0, y=0, x2=20, y2=20),
            graphic_state=il_version_1.GraphicState(
                passthrough_per_char_instruction="",
            ),
            pdf_path=[
                il_version_1.PdfPath(x="10.5", y="20.25", op="m", has_xy=True),
                il_version_1.PdfPath(x="15", y="25", op="l", has_xy=True),
            ],
            fill_background=False,
            stroke_path=True,
            evenodd=False,
            ctm=["1", "0", "0", "1", "2.5", "3.5"],
        )
        draw_op = BitStream()

        CurveRenderUnit(curve, render_order=1).render(draw_op, SimpleNamespace())

        self.assertIn(
            b"1.000000 0.000000 0.000000 1.000000 2.500000 3.500000 cm",
            draw_op.bytes,
        )
        self.assertIn(b"10.500000 20.250000 m", draw_op.bytes)

    def test_rectangle_render_unit_accepts_string_numeric_values(self) -> None:
        from bitstring import BitStream

        from babeldoc.format.pdf.document_il import il_version_1
        from babeldoc.format.pdf.document_il.backend.pdf_creater import (
            RectangleRenderUnit,
        )

        rectangle = il_version_1.PdfRectangle(
            box=il_version_1.Box(x="1", y="2", x2="5", y2="7"),
            graphic_state=il_version_1.GraphicState(
                passthrough_per_char_instruction="",
            ),
            fill_background=False,
            line_width="0.2",
        )
        draw_op = BitStream()

        RectangleRenderUnit(rectangle, render_order=1).render(
            draw_op,
            SimpleNamespace(),
        )

        self.assertIn(b"0.200000 w", draw_op.bytes)
        self.assertIn(b"1.000000 2.000000 4.000000 5.000000 re", draw_op.bytes)


class WorkspaceLockRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-lock-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_advance_reports_active_lock_as_structured_status(self) -> None:
        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_lock_metadata

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_lock_metadata(paths)

        result = advance(self.tmp)

        self.assertEqual(result["status"], "locked")
        self.assertIn("advance lock", result["validation_errors"][0])

    def test_advance_recovers_stale_lock_for_missing_process(self) -> None:
        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_json(
            paths.lock,
            {
                "pid": 99999999,
                "created_at": "2000-01-01T00:00:00",
                "created_at_epoch": 946684800.0,
                "workspace": str(self.tmp),
            },
        )

        result = advance(self.tmp)

        self.assertEqual(result["status"], "config_error")
        self.assertFalse(paths.lock.exists())


class ProgressPersistenceRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-progress-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_progress_callback_persists_latest_stage(self) -> None:
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_json(paths.state, {"status": "running"})

        _record_pipeline_progress(
            paths,
            {
                "type": "progress_update",
                "stage": "Parse Page Layout",
                "stage_progress": 25.0,
                "stage_current": 1,
                "stage_total": 4,
                "overall_progress": 12.5,
                "part_index": 1,
                "total_parts": 1,
            },
        )

        progress = read_json(paths.progress, None)
        state = read_json(paths.state, None)
        self.assertEqual(progress["stage"], "Parse Page Layout")
        self.assertEqual(progress["stage_current"], 1)
        self.assertNotIn("pipeline_progress", state)

    def test_file_task_pending_marks_stage_paused_instead_of_complete(self) -> None:
        from babeldoc.file_task_bridge import FileTaskPending
        from babeldoc.progress_monitor import ProgressMonitor
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_json(paths.state, {"status": "needs_ai_edit"})
        monitor = ProgressMonitor(
            [("Translate Paragraphs", 1.0)],
            progress_change_callback=lambda **event: _record_pipeline_progress(paths, event),
            report_interval=0,
        )

        with self.assertRaises(FileTaskPending):
            with monitor.stage_start("Translate Paragraphs", 2) as stage:
                stage.advance()
                raise FileTaskPending("task-hash")

        progress = read_json(paths.progress, None)
        state = read_json(paths.state, None)
        self.assertEqual(progress["event_type"], "progress_paused")
        self.assertEqual(progress["stage"], "Translate Paragraphs")
        self.assertEqual(progress["stage_current"], 1)
        self.assertLess(progress["stage_progress"], 100.0)
        self.assertTrue(progress["paused_for_ai"])
        self.assertNotIn("pipeline_progress", state)

    def test_memory_summary_is_persisted_with_progress(self) -> None:
        from file_task_pdf_translate.runner import _record_pipeline_progress
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        write_json(paths.state, {"status": "running"})

        _record_pipeline_progress(
            paths,
            {
                "type": "memory_summary",
                "peak_memory_mb": 123.4,
                "memory_stages": {
                    "load_model": 90.0,
                    "parse_layout": 101.0,
                    "translate": 111.0,
                    "font_subset": 120.0,
                    "pdf_save": 123.4,
                },
            },
        )

        progress = read_json(paths.progress, None)
        self.assertEqual(progress["peak_memory_mb"], 123.4)
        self.assertEqual(progress["memory_stages"]["font_subset"], 120.0)


class ProcessExitRegressionTests(unittest.TestCase):
    def test_priority_executor_import_does_not_block_process_exit(self) -> None:
        code = f"""
import sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
import babeldoc.utils.priority_thread_pool_executor
with ThreadPoolExecutor(max_workers=1) as executor:
    print(executor.submit(lambda: 1).result())
"""

        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(completed.stdout.strip(), "1")

    def test_advance_runs_runtime_cleanup_before_pending_response(self) -> None:
        from babeldoc.file_task_bridge import FileTaskPending
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import pending_snapshot_path
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        with tempfile.TemporaryDirectory(prefix="pdf-translate-exit-test-") as tmp:
            workspace = Path(tmp)
            write_test_pdf(workspace / "paper.pdf")
            (workspace / "assets").mkdir()
            (workspace / "pdf_translate.yaml").write_text(
                """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".lstrip(),
                encoding="utf-8",
            )

            def raise_pending(_config):
                paths = paths_for(workspace)
                state = read_json(paths.state, {})
                state["status"] = "needs_ai_edit"
                pending = {
                    "task_type": "translate",
                    "task_hash": "pending-cleanup",
                    "lang_out": "zh-CN",
                    "blocks": [
                        {
                            "source": "hello",
                            "original_source": "hello",
                            "required_placeholders": [],
                        }
                    ],
                }
                state["pending_task_hash"] = pending["task_hash"]
                write_json(pending_snapshot_path(paths, pending["task_hash"]), pending)
                paths.current_translation.write_text(
                    render_editable_document(
                        "translate",
                        [EditableBlock(source="hello")],
                        "zh-CN",
                    ),
                    encoding="utf-8",
                )
                write_json(paths.state, state)
                raise FileTaskPending("pending-cleanup")

            with patch(
                "file_task_pdf_translate.runner.set_runtime_asset_dir",
                return_value=workspace / "assets",
            ), patch(
                "file_task_pdf_translate.runner.translate",
                side_effect=raise_pending,
            ), patch(
                "file_task_pdf_translate.runner.shutdown_file_task_runtime",
            ) as cleanup:
                result = advance(workspace)

        self.assertEqual(result["status"], "needs_ai_edit")
        cleanup.assert_called_once()

    def test_advance_clears_stale_last_error_after_success(self) -> None:
        import pymupdf

        from file_task_pdf_translate.config import load_workspace_config
        from file_task_pdf_translate.runner import advance
        from file_task_pdf_translate.state import load_or_init_state
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        with tempfile.TemporaryDirectory(prefix="pdf-translate-done-state-test-") as tmp:
            workspace = Path(tmp)
            write_test_pdf(workspace / "paper.pdf")
            (workspace / "assets").mkdir()
            (workspace / "pdf_translate.yaml").write_text(
                """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
output_mode: mono
primary_font_family: null
add_formula_placehold_hint: true
""".lstrip(),
                encoding="utf-8",
            )
            paths = paths_for(workspace)
            config = load_workspace_config(workspace)
            state = load_or_init_state(paths, config.snapshot)
            state["last_error"] = "old failure"
            write_json(paths.state, state)

            output_pdf = workspace / "translated.pdf"
            doc = pymupdf.open()
            page = doc.new_page(width=300, height=120)
            page.insert_text((30, 60), "clean output")
            doc.save(output_pdf)
            doc.close()
            translate_result = SimpleNamespace(
                mono_pdf_path=output_pdf,
                dual_pdf_path=None,
            )

            with patch(
                "file_task_pdf_translate.runner.set_runtime_asset_dir",
                return_value=workspace / "assets",
            ), patch(
                "file_task_pdf_translate.runner.translate",
                return_value=translate_result,
            ), patch(
                "file_task_pdf_translate.runner.shutdown_file_task_runtime",
            ):
                result = advance(workspace)

            final_state = read_json(paths.state, {})

        self.assertEqual(result["status"], "done")
        self.assertNotIn("last_error", final_state)
        self.assertNotIn("output_pdf", final_state)
        self.assertEqual(result["progress"]["pipeline_progress"]["overall_progress"], 100.0)
        self.assertFalse(result["progress"]["pipeline_progress"]["paused_for_ai"])


if __name__ == "__main__":
    unittest.main()
