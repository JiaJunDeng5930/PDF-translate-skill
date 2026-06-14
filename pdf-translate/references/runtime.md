# Runtime Notes

The public interface is `scripts/advance.py` with no arguments. The current working directory is the PDF workspace.

Preparation-stage config:

- `pdf_translate.yaml`: AI-created workspace config before the first advance.
- `asset_dir`: local runtime asset directory prepared by `scripts/download_assets.py`.
- The first initialized state freezes the normalized config snapshot and `config_hash`.
- Later config drift returns `config_error`.
- Asset directory validation failures return `asset_error`.

Translation-stage state:

- `current_translation.yaml`: the only AI-editable file while a task is pending.
- `.pdf_translate/state.json`: business state only: frozen config, pending task hash, status, final output map, advance count, and `page_plan`.
- `.pdf_translate/progress.json`: latest pipeline progress snapshot.
- `.pdf_translate/tasks/`: pending task snapshots keyed by task hash.
- `.pdf_translate/accepted_answers/`: accepted editable files and JSON answers replayed into the internal PDF pipeline.
- `.pdf_translate/rejected_answers/`: damaged editable files archived before template restoration.
- `.pdf_translate/page_outputs/`: private per-page shard PDFs before page replacement into final outputs.
- `.pdf_translate/trace.jsonl`: compact config, progress, validation, answer, and output events.
- `.pdf_translate/advance.lock`: PID and timestamp metadata for the active advance process. A live PID returns `locked`; a missing PID is recovered as stale and traced before the run continues.

The runtime re-enters the internal PDF pipeline on each advance and replays accepted answers by stable task hash. The frozen `pages` config defines the target page set. `state.json` stores `page_plan.target_pages`, `page_plan.active_page`, and `page_plan.completed_pages` as the runtime cursor. BabelDOC receives a single-page `pages` value for the active shard. The file-task workflow has no preprocessing checkpoint layer; debug IL dumps use JSON only.

Each completed shard writes private PDFs under `.pdf_translate/page_outputs/`. The public output in `output/` is produced by replacing only `active_page` in the original or cumulative PDF with the corresponding page from the shard PDF. Pages outside `page_plan.target_pages` remain sourced from the original PDF.

The synchronous BabelDOC progress monitor writes the latest pipeline stage into `.pdf_translate/progress.json`. Stage starts, ends, and AI pauses are also recorded in `trace.jsonl`. `paused_for_ai` is derived from `status in {"needs_ai_edit", "needs_ai_fix"}`. Current UI progress should read `page_progress`, which is derived from the persisted page cursor. Historical trace events are audit data. A `done` state reports terminal progress with `overall_progress: 100` and `paused_for_ai: false`.

The BabelDOC-derived pipeline files with file-task changes are:

- `babeldoc/file_task_bridge.py`: pending exception and immediate executor.
- `babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py`: file-task sequential term extraction and pending propagation.
- `babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py`: file-task sequential translation batches and pending propagation.
- `babeldoc/format/pdf/high_level.py`: file-task pending propagation through the high-level pipeline.

Validation invariants:

- Editable task parsing uses YAML with top-level `task` and `items` fields.
- Item count, order, and every `source` field must match the saved snapshot.
- Translation tasks require every `translation` field to be non-empty.
- BabelDOC placeholders remain visible in editable YAML, for example `<b1>` and `</b1>`.
- Required placeholder sequences are derived from each block snapshot by exact `<bN>` scanning.
- Validation compares the saved snapshot sequence with each translation sequence and reports the first mismatch position, expected item, actual item, and local windows.
- Accepted translation answers replay the translation text as written, including required placeholders.
- Completed output PDFs are text-scanned for visible `<bN>`, `</bN>`, `{{FORMULA_N}}`, and `{{PROTECTED_N}}` markers before `state.json` is marked `done`.
- Term extraction tasks require `terms` to be a YAML list of mappings with
  `source` and `target` fields. Source term matching normalizes PDF line-break
  hyphenation, abnormal whitespace, and Unicode compatibility forms. Each
  `source` term must still be an exact contiguous span after that normalization.

PDF generation is owned by the internal pipeline: `high_level.translate()` runs layout parsing, paragraph finding, styles/formulas, term extraction, IL translation, typesetting, font mapping, and PDF creation.

The PDF preprocessing path preserves page annotations and links. Normalization helpers may rewrite streams and xrefs, but `/Annots` entries remain attached to their pages.

Runtime assets are prepared outside translation:

- `scripts/download_assets.py <asset-dir>` downloads the DocLayout ONNX model, fonts, CMap files, and tiktoken cache into the configured directory.
- `manifest.json` records the expected asset names and SHA3-256 hashes.
- If `<asset-dir>` already validates against `manifest.json`, `download_assets.py` returns without contacting upstreams. Individual model, font, CMap, and tiktoken groups also skip upstream selection when every expected file already matches its hash.
- `advance` activates the frozen `asset_dir`, validates `manifest.json` and every required file, then runs the PDF pipeline.
- After `asset_dir` is active, the BabelDOC-derived asset loader reads local files and raises `AssetError` for missing or damaged assets.
