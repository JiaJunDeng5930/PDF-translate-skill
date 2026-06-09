from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "pdf-translate" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf-translate-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

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
version: 1
input_pdf: paper.pdf
lang_in: en
lang_out: ja
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
        config = _build_translation_config(paths_for(self.tmp), state, translator)

        self.assertEqual(translator.lang_in, "en")
        self.assertEqual(translator.lang_out, "ja")
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
version: 1
input_pdf: paper.pdf
lang_in: en
lang_out: zh-CN
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
version: 1
input_pdf: paper.pdf
lang_in: en
lang_out: ja
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
    def test_empty_multi_block_translation_sections_remain_distinct_blocks(self) -> None:
        from file_task_pdf_translate.editable import END_MARKER
        from file_task_pdf_translate.editable import SOURCE_MARKER
        from file_task_pdf_translate.editable import TRANSLATION_MARKER
        from file_task_pdf_translate.editable import parse_blocks

        text = "\n".join(
            [
                SOURCE_MARKER,
                "alpha",
                TRANSLATION_MARKER,
                END_MARKER,
                SOURCE_MARKER,
                "beta",
                TRANSLATION_MARKER,
                END_MARKER,
                "",
            ],
        )

        blocks, errors = parse_blocks(text)

        self.assertEqual(errors, [])
        self.assertEqual([block.source for block in blocks], ["alpha", "beta"])
        self.assertEqual([block.translation for block in blocks], ["", ""])


if __name__ == "__main__":
    unittest.main()
