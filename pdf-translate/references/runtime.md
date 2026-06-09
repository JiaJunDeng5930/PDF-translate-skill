# Runtime Notes

The public interface is `scripts/advance.py` with no arguments. The current working directory is the PDF workspace.

Preparation-stage config:

- `pdf_translate.yaml`: AI-created workspace config before the first advance.
- `asset_dir`: local runtime asset directory prepared by `scripts/download_assets.py`.
- The first initialized state freezes the normalized config snapshot and `config_hash`.
- Later config drift returns `config_error`.
- Asset directory validation failures return `asset_error`.

Translation-stage state:

- `current_translation.txt`: the only AI-editable file while a task is pending.
- `.pdf_translate/state.json`: frozen config, pending task, accepted answer index, final outputs.
- `.pdf_translate/tasks/`: snapshots for pending tasks.
- `.pdf_translate/accepted_answers/`: accepted editable files and JSON answers replayed into the internal PDF pipeline.
- `.pdf_translate/rejected_answers/`: damaged editable files archived before template restoration.
- `.pdf_translate/trace.jsonl`: compact config, progress, validation, answer, and output events.

The runtime re-enters the internal PDF pipeline from the beginning on each advance and replays accepted answers by stable task hash. This keeps the workflow resumable while keeping checkpoints program-owned.

The BabelDOC-derived pipeline files with file-task changes are:

- `babeldoc/file_task_bridge.py`: pending exception and immediate executor.
- `babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py`: file-task sequential term extraction and pending propagation.
- `babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py`: file-task sequential translation batches and pending propagation.
- `babeldoc/format/pdf/high_level.py`: file-task pending propagation through the high-level pipeline.

Validation invariants:

- Text block parsing is line-state based, so empty translation blocks remain valid blocks.
- Source block count, order, and content must match the saved snapshot.
- Translation tasks require every translation block to be non-empty.
- Protected marker sequence must match the source block sequence.
- Term extraction pairs must parse as `source ? target`, and the source term must occur in the matching source block.

PDF generation is owned by the internal pipeline: `high_level.translate()` runs layout parsing, paragraph finding, styles/formulas, term extraction, IL translation, typesetting, font mapping, and PDF creation.

Runtime assets are prepared outside translation:

- `scripts/download_assets.py <asset-dir>` downloads the DocLayout ONNX model, fonts, CMap files, and tiktoken cache into the configured directory.
- `manifest.json` records the expected asset names and SHA3-256 hashes.
- `advance` activates the frozen `asset_dir`, validates `manifest.json` and every required file, then runs the PDF pipeline.
- After `asset_dir` is active, the BabelDOC-derived asset loader reads local files and raises `AssetError` for missing or damaged assets.
