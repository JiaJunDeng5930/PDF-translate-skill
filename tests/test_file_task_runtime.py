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
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "pdf-translate" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


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
        self.assertIn("watermark_output_mode must be one of", message)
        self.assertIn("primary_font_family must be", message)

    def test_config_maps_language_output_and_watermark_to_babeldoc_params(self) -> None:
        from babeldoc.format.pdf.translation_config import WatermarkOutputMode
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
watermark_output_mode: both
auto_extract_glossary: false
primary_font_family: serif
add_formula_placehold_hint: false
""".strip()
            + "\n",
        )
        snapshot = load_workspace_config(self.tmp).snapshot
        state = {
            "config": snapshot,
            "config_hash": snapshot["config_hash"],
            "accepted": {},
        }
        translator = FileTaskTranslator(paths_for(self.tmp), state)
        with patch(
            "babeldoc.docvision.doclayout.DocLayoutModel.load_available",
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
        self.assertEqual(config.watermark_output_mode, WatermarkOutputMode.Both)
        self.assertFalse(config.auto_extract_glossary)
        self.assertEqual(config.primary_font_family, "serif")
        self.assertFalse(config.add_formula_placehold_hint)

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
watermark_output_mode: watermarked
auto_extract_glossary: true
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
watermark_output_mode: watermarked
auto_extract_glossary: true
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
watermark_output_mode: watermarked
auto_extract_glossary: true
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


class OutputSelectionTests(unittest.TestCase):
    def test_output_pdfs_exposes_every_generated_variant_and_primary_path(self) -> None:
        from file_task_pdf_translate.runner import _collect_output_pdfs

        result = SimpleNamespace(
            mono_pdf_path=Path("out/watermarked.mono.pdf"),
            dual_pdf_path=Path("out/watermarked.dual.pdf"),
            no_watermark_mono_pdf_path=Path("out/clean.mono.pdf"),
            no_watermark_dual_pdf_path=Path("out/clean.dual.pdf"),
        )
        output_pdfs, primary = _collect_output_pdfs(
            result,
            {
                "output_mode": "dual",
                "watermark_output_mode": "no_watermark",
            },
        )

        self.assertNotIn("watermarked_dual", output_pdfs)
        self.assertNotIn("no_watermark_mono", output_pdfs)
        self.assertEqual(output_pdfs["no_watermark_dual"], "out\\clean.dual.pdf")
        self.assertEqual(primary, "out\\clean.dual.pdf")


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

    def test_term_yaml_uses_structured_pairs(self) -> None:
        from file_task_pdf_translate.editable import parse_editable_document

        document, errors = parse_editable_document(
            """
task: term_extract
target_language: zh-CN
items:
  - id: 1
    source: weather labels
    terms:
      - source: snow
        target: snow-target
      - source: fall
        target: fall-target
""".lstrip()
        )

        self.assertEqual(errors, [])
        self.assertIsNotNone(document)
        assert document is not None
        self.assertEqual(
            [(pair.source, pair.target) for pair in document.items[0].terms],
            [("snow", "snow-target"), ("fall", "fall-target")],
        )

    def test_legacy_term_separator_string_is_rejected(self) -> None:
        from file_task_pdf_translate.editable import parse_editable_document

        document, errors = parse_editable_document(
            """
task: term_extract
items:
  - id: 1
    source: snow
    terms: snow -> snow-target
""".lstrip()
        )

        self.assertIsNone(document)
        self.assertEqual(errors, ["item 1: terms must be a YAML list"])

    def test_protected_tokens_are_plain_text_yaml_values(self) -> None:
        from file_task_pdf_translate.editable import marker_sequence
        from file_task_pdf_translate.editable import replace_internal_placeholders
        from file_task_pdf_translate.editable import restore_internal_placeholders

        display, token_map = replace_internal_placeholders(
            "Use <b0> and <b1>.",
            {"<b0>"},
        )

        self.assertEqual(display, "Use {{FORMULA_1}} and {{PROTECTED_1}}.")
        self.assertEqual(marker_sequence(display), ["FORMULA_1", "PROTECTED_1"])
        self.assertEqual(
            restore_internal_placeholders(display, token_map),
            "Use <b0> and <b1>.",
        )
        self.assertNotIn("[[", display)
        self.assertNotIn("⟦", display)

    def test_protected_tokens_are_detected_when_adjacent_to_text_and_digits(self) -> None:
        from file_task_pdf_translate.editable import marker_sequence
        from file_task_pdf_translate.editable import replace_internal_placeholders
        from file_task_pdf_translate.editable import restore_internal_placeholders

        display, token_map = replace_internal_placeholders("<b6></b7>3<b8>")

        self.assertEqual(display, "{{PROTECTED_1}}{{PROTECTED_2}}3{{PROTECTED_3}}")
        self.assertEqual(
            marker_sequence(display),
            ["PROTECTED_1", "PROTECTED_2", "PROTECTED_3"],
        )
        self.assertEqual(
            marker_sequence("Liangsi LuFORMULA_1Xuhang Chen"),
            ["FORMULA_1"],
        )
        self.assertEqual(
            marker_sequence("FORMULA_1PROTECTED_1Corresponding author"),
            ["FORMULA_1", "PROTECTED_1"],
        )
        self.assertEqual(
            marker_sequence(" PROTECTED_1ChordEditPROTECTED_2,"),
            ["PROTECTED_1", "PROTECTED_2"],
        )
        self.assertEqual(
            restore_internal_placeholders(
                "FORMULA_1PROTECTED_1Corresponding author",
                [
                    {"marker": "FORMULA_1", "token": "<b0>"},
                    {"marker": "PROTECTED_1", "token": "<b1>"},
                ],
            ),
            "<b0><b1>Corresponding author",
        )
        self.assertEqual(
            restore_internal_placeholders(display, token_map),
            "<b6></b7>3<b8>",
        )


class TermValidationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-term-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_term_source_matching_normalizes_pdf_hyphenation(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import TermPair
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source = (
            "We recast editing as a transport problem between the source and "
            "target distribu- tions. ChordEdit achieves true real- time editing."
        )
        pending = {
            "task_type": "term_extract",
            "task_hash": "terms-normalized",
            "blocks": [
                {
                    "source": source,
                    "original_source": source,
                    "token_map": [],
                    "required_markers": [],
                }
            ],
        }
        state = {
            "pending": pending,
            "status": "needs_ai_edit",
            "accepted": {},
        }
        write_json(paths.state, state)
        paths.current_translation.write_text(
            render_editable_document(
                "term_extract",
                [
                    EditableBlock(
                        source=source,
                        terms=[
                            TermPair(
                                source="source and target distributions",
                                target="source-target distribution",
                            ),
                            TermPair(
                                source="real-time editing",
                                target="realtime editing",
                            ),
                        ],
                    )
                ],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        result = validate_pending(paths, state)

        self.assertTrue(result.accepted)
        answer = read_json(paths.accepted / "terms-normalized.answer.json", None)
        self.assertEqual(
            answer,
            [
                {
                    "src": "source and target distributions",
                    "tgt": "source-target distribution",
                },
                {"src": "real-time editing", "tgt": "realtime editing"},
            ],
        )


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
            "accepted": {},
            "pending": None,
            "status": "initialized",
        }
        write_json(paths.state, state)
        translator = FileTaskTranslator(paths, state)

        with self.assertRaises(FileTaskPending):
            translator.do_translate("hello world")

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
        self.assertEqual(document.items[0].source, "hello world")

        state = read_json(paths.state, None)
        paths.current_translation.write_text(
            render_editable_document(
                "translate",
                [EditableBlock(source="hello world", translation="你好，世界")],
                "zh-CN",
            ),
            encoding="utf-8",
        )

        task_hash = state["pending"]["task_hash"]
        result = validate_pending(paths, state)

        self.assertTrue(result.accepted)
        accepted = read_json(
            paths.accepted / f"{task_hash}.answer.json",
            None,
        )
        self.assertEqual(accepted, [{"id": 0, "output": "你好，世界"}])


    def test_translation_validation_rejects_snapshot_markers_missing_from_token_map(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source = "Eq PROTECTED_2PROTECTED_34.5"
        pending = {
            "task_type": "translate",
            "task_hash": "ambiguous-marker",
            "blocks": [
                {
                    "source": source,
                    "original_source": "<b1></b1>4.5",
                    "token_map": [
                        {"marker": "PROTECTED_2", "token": "<b1>"},
                        {"marker": "PROTECTED_3", "token": "</b1>"},
                    ],
                    "required_markers": ["PROTECTED_2", "PROTECTED_34"],
                }
            ],
        }
        state = {"pending": pending, "status": "needs_ai_edit", "accepted": {}}
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

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "needs_ai_fix")
        self.assertIn("PROTECTED_34", "\n".join(result.errors))

    def test_translation_validation_rejects_unbalanced_restored_internal_tags(self) -> None:
        from file_task_pdf_translate.editable import EditableBlock
        from file_task_pdf_translate.editable import render_editable_document
        from file_task_pdf_translate.state import ensure_dirs
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import write_json
        from file_task_pdf_translate.validation import validate_pending

        paths = paths_for(self.tmp)
        ensure_dirs(paths)
        source = "PROTECTED_1 text"
        pending = {
            "task_type": "translate",
            "task_hash": "unbalanced-marker",
            "blocks": [
                {
                    "source": source,
                    "original_source": "</b1> text",
                    "token_map": [{"marker": "PROTECTED_1", "token": "</b1>"}],
                    "required_markers": ["PROTECTED_1"],
                }
            ],
        }
        state = {"pending": pending, "status": "needs_ai_edit", "accepted": {}}
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

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, "needs_ai_fix")
        self.assertIn("unmatched closing tag", "\n".join(result.errors))


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
        write_json(paths.state, {"status": "running", "accepted": {}})

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

        state = read_json(paths.state, None)
        self.assertEqual(state["pipeline_progress"]["stage"], "Parse Page Layout")
        self.assertEqual(state["pipeline_progress"]["stage_current"], 1)

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
        write_json(paths.state, {"status": "running", "accepted": {}})
        monitor = ProgressMonitor(
            [("Translate Paragraphs", 1.0)],
            progress_change_callback=lambda **event: _record_pipeline_progress(paths, event),
            report_interval=0,
        )

        with self.assertRaises(FileTaskPending):
            with monitor.stage_start("Translate Paragraphs", 2) as stage:
                stage.advance()
                raise FileTaskPending("task-hash")

        state = read_json(paths.state, None)
        self.assertEqual(state["pipeline_progress"]["event_type"], "progress_paused")
        self.assertEqual(state["pipeline_progress"]["stage"], "Translate Paragraphs")
        self.assertEqual(state["pipeline_progress"]["stage_current"], 1)
        self.assertLess(state["pipeline_progress"]["stage_progress"], 100.0)


class PreprocessCacheRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-cache-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_file_task_preprocess_cache_restores_latest_saved_stage(self) -> None:
        from babeldoc.format.pdf.high_level import _load_file_task_preprocess_cache
        from babeldoc.format.pdf.high_level import _save_file_task_preprocess_cache

        source_pdf = self.tmp / "paper.pdf"
        source_pdf.write_bytes(b"source-pdf")
        normalized_pdf = self.tmp / "input.pdf"
        normalized_pdf.write_bytes(b"normalized-pdf")
        config = SimpleNamespace(
            file_task_workflow=True,
            file_task_preprocess_cache_key="config-hash",
            input_file=source_pdf,
            only_parse_generate_pdf=False,
            split_strategy=None,
            working_dir=self.tmp / "work",
        )

        class Converter:
            def write_xml(self, document, path):
                Path(path).write_text(document["payload"], encoding="utf-8")

            def read_xml(self, path):
                return {"payload": Path(path).read_text(encoding="utf-8")}

        converter = Converter()
        _save_file_task_preprocess_cache(
            config,
            converter,
            "styles_and_formulas",
            normalized_pdf,
            {"payload": "cached-docs"},
            {7: {"MediaBox": "[0 0 100 100]"}},
        )

        cached = _load_file_task_preprocess_cache(config, converter)

        self.assertIsNotNone(cached)
        self.assertEqual(cached["stage"], "styles_and_formulas")
        self.assertEqual(cached["docs"], {"payload": "cached-docs"})
        self.assertEqual(cached["mediabox_data"], {7: {"MediaBox": "[0 0 100 100]"}})
        self.assertEqual(cached["temp_pdf_path"].read_bytes(), b"normalized-pdf")


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
        from file_task_pdf_translate.state import paths_for
        from file_task_pdf_translate.state import read_json
        from file_task_pdf_translate.state import write_json

        with tempfile.TemporaryDirectory(prefix="pdf-translate-exit-test-") as tmp:
            workspace = Path(tmp)
            (workspace / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            (workspace / "assets").mkdir()
            (workspace / "pdf_translate.yaml").write_text(
                """
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
asset_dir: assets
output_mode: mono
watermark_output_mode: watermarked
auto_extract_glossary: false
primary_font_family: null
add_formula_placehold_hint: true
""".lstrip(),
                encoding="utf-8",
            )

            def raise_pending(_config):
                paths = paths_for(workspace)
                state = read_json(paths.state, {})
                state["status"] = "needs_ai_edit"
                state["pending"] = {
                    "task_type": "translate",
                    "task_hash": "pending-cleanup",
                    "lang_out": "zh-CN",
                    "blocks": [
                        {
                            "source": "hello",
                            "original_source": "hello",
                            "token_map": [],
                            "required_markers": [],
                        }
                    ],
                }
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


if __name__ == "__main__":
    unittest.main()
