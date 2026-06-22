import logging
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from babeldoc.const import CACHE_FOLDER
from babeldoc.glossary import Glossary
from babeldoc.progress_monitor import ProgressMonitor
from babeldoc.translator.translator import BaseTranslator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TitleContextSnapshot:
    debug_id: str | None
    unicode: str | None
    layout_label: str | None = None


class SharedContextCrossSplitPart:
    def __init__(self):
        self.first_paragraph: TitleContextSnapshot | None = None
        self.recent_title_paragraph: TitleContextSnapshot | None = None
        self._lock = threading.Lock()
        self.user_glossaries: list[Glossary] = []
        self.auto_enabled_ocr_workaround = False
        # Statistics for valid characters/text across the whole file
        self.valid_char_count_total: int = 0
        self.total_valid_text_token_count: int = 0

    def snapshot_title_paragraph(self, paragraph) -> TitleContextSnapshot | None:
        if paragraph is None:
            return None
        return TitleContextSnapshot(
            debug_id=getattr(paragraph, "debug_id", None),
            unicode=getattr(paragraph, "unicode", None),
            layout_label=getattr(paragraph, "layout_label", None),
        )

    def initialize_glossaries(self, initial_glossaries: list[Glossary] | None):
        with self._lock:
            self.user_glossaries = (
                list(initial_glossaries) if initial_glossaries else []
            )
            # reset statistics buffer when initializing
            self.valid_char_count_total = 0
            self.total_valid_text_token_count = 0

    def get_glossaries(self) -> list[Glossary]:
        with self._lock:
            return list(self.user_glossaries)

    def add_valid_counts(self, char_count: int, token_count: int):
        """Accumulate valid character and token counts in a threadsafe way."""
        if char_count <= 0 and token_count <= 0:
            return
        with self._lock:
            if char_count > 0:
                self.valid_char_count_total += char_count
            if token_count > 0:
                self.total_valid_text_token_count += token_count


class TranslationConfig:
    def __init__(
        self,
        translator: BaseTranslator,
        input_file: str | Path,
        lang_in: str,
        lang_out: str,
        doc_layout_model,
        pages: str | None = None,
        output_dir: str | Path | None = None,
        debug: bool = False,
        working_dir: str | Path | None = None,
        no_dual: bool = False,
        no_mono: bool = False,
        formular_font_pattern: str | None = None,
        formular_char_pattern: str | None = None,
        qps: int = 1,
        split_short_lines: bool = False,
        short_line_split_factor: float = 0.8,
        use_rich_pbar: bool = True,
        progress_monitor: ProgressMonitor | None = None,
        skip_clean: bool = False,
        dual_translate_first: bool = False,
        disable_rich_text_translate: bool = False,
        enhance_compatibility: bool = False,
        report_interval: float = 0.1,
        min_text_length: int = 5,
        use_side_by_side_dual: bool = True,  # Deprecated: 是否使用拼版式双语 PDF（并排显示原文和译文）向下兼容选项，已停用。
        use_alternating_pages_dual: bool = False,
        # Add split-related parameters
        show_char_box: bool = False,
        skip_scanned_detection: bool = False,
        ocr_workaround: bool = False,
        custom_system_prompt: str | None = None,
        add_formula_placehold_hint: bool = False,
        glossaries: list[Glossary] | None = None,
        pool_max_workers: int | None = None,
        auto_enable_ocr_workaround: bool = False,
        primary_font_family: str | None = None,
        only_include_translated_page: bool | None = False,
        enable_graphic_element_process: bool = True,
        merge_alternating_line_numbers: bool = True,
        skip_translation: bool = False,
        skip_form_render: bool = False,
        skip_curve_render: bool = False,
        only_parse_generate_pdf: bool = False,
        remove_non_formula_lines: bool = False,
        non_formula_line_iou_threshold: float = 0.9,
        figure_table_protection_threshold: float = 0.9,
        skip_formula_offset_calculation: bool = False,
        metadata_extra_data: str | None = None,
        disable_same_text_fallback: bool = False,
    ):
        self.translator = translator
        initial_user_glossaries = list(glossaries) if glossaries else []

        self.input_file = input_file
        self.lang_in = lang_in
        self.lang_out = lang_out

        self.pages = pages
        self.page_ranges = self.parse_pages(pages) if pages else None
        self.debug = debug

        self.output_dir = output_dir
        self.working_dir = working_dir
        self.no_dual = no_dual
        self.no_mono = no_mono

        self.formular_font_pattern = formular_font_pattern
        self.formular_char_pattern = formular_char_pattern
        self.qps = qps
        # Set pool_max_workers with default value from qps
        self.pool_max_workers = (
            pool_max_workers if pool_max_workers is not None else qps
        )
        self.split_short_lines = split_short_lines

        self.short_line_split_factor = short_line_split_factor
        self.use_rich_pbar = use_rich_pbar
        self.progress_monitor = progress_monitor
        self.doc_layout_model = doc_layout_model

        self.skip_clean = skip_clean or enhance_compatibility
        self.skip_scanned_detection = skip_scanned_detection

        self.dual_translate_first = dual_translate_first or enhance_compatibility
        self.disable_rich_text_translate = (
            disable_rich_text_translate or enhance_compatibility
        )

        self.report_interval = report_interval
        self.min_text_length = min_text_length
        self.use_alternating_pages_dual = use_alternating_pages_dual
        self.ocr_workaround = ocr_workaround
        self.merge_alternating_line_numbers = merge_alternating_line_numbers

        if self.ocr_workaround:
            self.skip_scanned_detection = True
            self.disable_rich_text_translate = True

        # for backward compatibility
        if use_side_by_side_dual is False and use_alternating_pages_dual is False:
            self.use_alternating_pages_dual = True

        if progress_monitor and progress_monitor.cancel_event is None:
            progress_monitor.cancel_event = threading.Event()

        if working_dir is None:
            if debug:
                working_dir = Path(CACHE_FOLDER) / "working" / Path(input_file).stem
                self._is_temp_dir = False
            else:
                working_dir = tempfile.mkdtemp()
                self._is_temp_dir = True
        else:
            working_dir = Path(working_dir) / Path(input_file).stem
            self._is_temp_dir = False

        self.working_dir = working_dir

        Path(working_dir).mkdir(parents=True, exist_ok=True)

        if output_dir is None:
            output_dir = Path.cwd()
        self.output_dir = output_dir

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        if not doc_layout_model:
            from babeldoc.docvision.doclayout import OnnxModel

            doc_layout_model = OnnxModel.load_available()
        self.doc_layout_model = doc_layout_model

        self.shared_context_cross_split_part = SharedContextCrossSplitPart()
        self.shared_context_cross_split_part.initialize_glossaries(
            initial_user_glossaries
        )

        self.show_char_box = show_char_box
        self.custom_system_prompt = custom_system_prompt
        self.add_formula_placehold_hint = add_formula_placehold_hint
        self.auto_enable_ocr_workaround = auto_enable_ocr_workaround
        self.skip_translation = skip_translation
        self.only_parse_generate_pdf = only_parse_generate_pdf

        if auto_enable_ocr_workaround:
            self.ocr_workaround = False
            self.skip_scanned_detection = False

        assert primary_font_family in [
            None,
            "serif",
            "sans-serif",
            "script",
        ]
        self.primary_font_family = primary_font_family

        if only_include_translated_page is None:
            only_include_translated_page = False

        self.only_include_translated_page = only_include_translated_page

        self.enable_graphic_element_process = enable_graphic_element_process
        self.skip_form_render = skip_form_render
        self.skip_curve_render = skip_curve_render
        self.remove_non_formula_lines = remove_non_formula_lines
        self.non_formula_line_iou_threshold = non_formula_line_iou_threshold
        self.figure_table_protection_threshold = figure_table_protection_threshold
        self.skip_formula_offset_calculation = skip_formula_offset_calculation

        self.metadata_extra_data = metadata_extra_data
        self.disable_same_text_fallback = disable_same_text_fallback

        if self.ocr_workaround:
            self.remove_non_formula_lines = False

    def parse_pages(self, pages_str: str | None) -> list[tuple[int, int]] | None:
        """解析页码字符串，返回页码范围列表

        Args:
            pages_str: 形如 "1-,2,-3,4" 的页码字符串

        Returns:
            包含 (start, end) 元组的列表，其中 -1 表示无限制
        """
        if not pages_str:
            return None

        ranges: list[tuple[int, int]] = []
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-")
                start_as_int = int(start) if start else 1
                end_as_int = int(end) if end else -1
                ranges.append((start_as_int, end_as_int))
            else:
                page = int(part)
                ranges.append((page, page))
        return ranges

    def should_translate_page(self, page_number: int) -> bool:
        """判断指定页码是否需要翻译
        Args:
            page_number: 页码
        Returns:
            是否需要翻译该页
        """
        if isinstance(self.page_ranges, list) and len(self.page_ranges) == 0:
            return False
        if not self.page_ranges:
            return True

        for start, end in self.page_ranges:
            if start <= page_number and (end == -1 or page_number <= end):
                return True
        return False

    def get_output_file_path(self, filename: str) -> Path:
        return Path(self.output_dir) / filename

    def get_working_file_path(self, filename: str) -> Path:
        return Path(self.working_dir) / filename

    def cleanup_temp_files(self):
        try:
            if self._is_temp_dir:
                logger.info(f"cleanup temp files: {self.working_dir}")
                shutil.rmtree(self.working_dir, ignore_errors=True)
        except Exception:
            logger.exception("Error cleaning up temporary files")

    def raise_if_cancelled(self):
        if self.progress_monitor is not None:
            self.progress_monitor.raise_if_cancelled()

    def cancel_translation(self):
        if self.progress_monitor is not None:
            self.progress_monitor.cancel()


class TranslateResult:
    original_pdf_path: str
    total_seconds: float
    mono_pdf_path: Path | None
    dual_pdf_path: Path | None
    peak_memory_usage: int | None
    total_valid_character_count: int | None
    total_valid_text_token_count: int | None

    def __init__(
        self,
        mono_pdf_path: Path | None,
        dual_pdf_path: Path | None,
    ):
        self.mono_pdf_path = mono_pdf_path
        self.dual_pdf_path = dual_pdf_path

        self.total_valid_character_count = None
        self.total_valid_text_token_count = None

    def __str__(self):
        """Return a human-readable string representation of the translation result."""
        result = []
        if hasattr(self, "original_pdf_path") and self.original_pdf_path:
            result.append(f"\tOriginal PDF: {self.original_pdf_path}")

        if hasattr(self, "total_seconds") and self.total_seconds:
            result.append(f"\tTotal time: {self.total_seconds:.2f} seconds")

        if self.mono_pdf_path:
            result.append(f"\tMonolingual PDF: {self.mono_pdf_path}")

        if self.dual_pdf_path:
            result.append(f"\tDual-language PDF: {self.dual_pdf_path}")

        if hasattr(self, "peak_memory_usage") and self.peak_memory_usage:
            result.append(f"\tPeak memory usage: {self.peak_memory_usage} MB")

        if hasattr(self, "total_valid_character_count") and isinstance(
            self.total_valid_character_count, int
        ):
            result.append(
                f"\tTotal valid character count: {self.total_valid_character_count}"
            )

        if hasattr(self, "total_valid_text_token_count") and isinstance(
            self.total_valid_text_token_count, int
        ):
            result.append(
                f"\tTotal valid text token count (gpt-4o): {self.total_valid_text_token_count}"
            )

        if result:
            result.insert(0, "Translation results:")

        return "\n".join(result) if result else "No translation results available"
