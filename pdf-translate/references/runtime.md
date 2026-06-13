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
- `.pdf_translate/state.json`: business state only: frozen config, pending task hash, status, final output map, and advance count.
- `.pdf_translate/progress.json`: latest pipeline progress snapshot.
- `.pdf_translate/tasks/`: pending task snapshots keyed by task hash.
- `.pdf_translate/accepted_answers/`: accepted editable files and JSON answers replayed into the internal PDF pipeline.
- `.pdf_translate/rejected_answers/`: damaged editable files archived before template restoration.
- `.pdf_translate/trace.jsonl`: compact config, progress, validation, answer, and output events.
- `.pdf_translate/advance.lock`: PID and timestamp metadata for the active advance process. A live PID returns `locked`; a missing PID is recovered as stale and traced before the run continues.

The runtime re-enters the internal PDF pipeline on each advance and replays accepted answers by stable task hash. File-task preprocessing checkpoints live under `.pdf_translate/babeldoc_work/<input-stem>/file_task_preprocess_cache/` and store the normalized input PDF, mediabox data, and IL XML after completed preprocessing stages. A cache hit with matching `config_hash` and input PDF hash resumes after the latest saved stage.

The synchronous BabelDOC progress monitor writes the latest pipeline stage into `.pdf_translate/progress.json`. Stage starts, ends, and AI pauses are also recorded in `trace.jsonl`. `paused_for_ai` is derived from `status in {"needs_ai_edit", "needs_ai_fix"}`. A `done` state reports terminal progress with `overall_progress: 100` and `paused_for_ai: false`.

The BabelDOC-derived pipeline files with file-task changes are:

- `babeldoc/file_task_bridge.py`: pending exception and immediate executor.
- `babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py`: file-task sequential term extraction and pending propagation.
- `babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py`: file-task sequential translation batches and pending propagation.
- `babeldoc/format/pdf/high_level.py`: file-task pending propagation and preprocessing checkpoints through the high-level pipeline.

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
  hyphenation and abnormal whitespace.

PDF generation is owned by the internal pipeline: `high_level.translate()` runs layout parsing, paragraph finding, styles/formulas, term extraction, IL translation, typesetting, font mapping, and PDF creation.

The PDF preprocessing path preserves page annotations and links. Normalization helpers may rewrite streams and xrefs, but `/Annots` entries remain attached to their pages.

Runtime assets are prepared outside translation:

- `scripts/download_assets.py <asset-dir>` downloads the DocLayout ONNX model, fonts, CMap files, and tiktoken cache into the configured directory.
- `manifest.json` records the expected asset names and SHA3-256 hashes.
- If `<asset-dir>` already validates against `manifest.json`, `download_assets.py` returns without contacting upstreams. Individual model, font, CMap, and tiktoken groups also skip upstream selection when every expected file already matches its hash.
- `advance` activates the frozen `asset_dir`, validates `manifest.json` and every required file, then runs the PDF pipeline.
- After `asset_dir` is active, the BabelDOC-derived asset loader reads local files and raises `AssetError` for missing or damaged assets.
