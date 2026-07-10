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
- `.pdf_translate/state.json`: compact business state: frozen config, pending task hash, status, final output map, counters, `page_plan`, and optional `output_plan`.
- `.pdf_translate/progress.json`: latest pipeline progress snapshot.
- `.pdf_translate/tasks/`: pending task snapshots keyed by task hash.
- `.pdf_translate/accepted_answers/`: accepted editable files and JSON answers replayed into the internal PDF pipeline.
- `.pdf_translate/rejected_answers/`: damaged editable files archived before template restoration.
- `.pdf_translate/page_sources/`: cached one-page source PDFs keyed by source page.
- `.pdf_translate/page_outputs/`: validated one-page mono and dual pipeline outputs.
- `.pdf_translate/assembled/`: private PDFs updated one page at a time before publication.
- `.pdf_translate/trace.jsonl`: compact config, progress, validation, answer, and output events.
- `.pdf_translate/advance.lock`: PID and timestamp metadata for the active advance process. A live PID returns `locked`; a missing PID is recovered as stale and traced before the run continues.

The frozen `pages` config defines the target page set. `state.json` stores the compact cursor `page_plan.target_page_ranges`, `target_count`, `active_page`, and `completed_count`; large contiguous ranges stay constant-sized. The first run also freezes the source PDF size, modification timestamp, and page count. Later source drift returns `config_error` before cached pages can be mixed with a changed input.

Each selected-page advance gives BabelDOC a cached one-page PDF with `pages: "1"` while `page_plan.active_page` retains the original 1-based page identity. Accepted answers are durable translation patches keyed by stable task hash. When every task for the active page has an accepted answer, the same synchronous run completes typesetting, font mapping, ToUnicode repair, and one-page PDF generation. The runtime validates that page output, incrementally commits mono content into a private copy of the source PDF, and advances the target cursor.

Dual and `both` modes use a second compact cursor in `output_plan`. After selected pages are translated, each `advance` assembles one source page: selected pages use validated dual shards, while unselected pages use the original page on both sides. Mono-only work publishes immediately after the final target page. Public files appear in `output/` only at `done`; every intermediate PDF stays under `.pdf_translate/`. All page and output transitions occur inside the foreground `advance` process.

Heavy parsing, layout, translation replay, typesetting, and PDF creation are bounded to one page per command. Page cursors, accepted-answer counts, progress, and trace-tail reads remain compact as page count and history grow. Initial mono assembly copies the source bytes once, and PDF opens retain the source xref metadata required for random page access. Incremental PDF revisions keep later commits bounded and preserve the original page objects, catalog, links, annotations, and outlines.

The synchronous BabelDOC progress monitor writes the latest pipeline stage into `.pdf_translate/progress.json`. Stage starts, ends, AI pauses, selected-page completion, output-page completion, and memory summaries are also recorded in `trace.jsonl`. `workflow_progress` combines committed target pages with committed output-assembly pages. Running stages and pending AI tasks keep active-page stage percentages in `active_page_stage_progress`; committed cursors alone advance workflow progress. `paused_for_ai` is derived from `status in {"needs_ai_edit", "needs_ai_fix"}`. Historical trace events are audit data, and response tails read only the final records.

The BabelDOC-derived pipeline files with file-task changes are:

- `babeldoc/file_task_bridge.py`: pending exception and immediate executor.
- `babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py`: file-task page translation tasks and pending propagation.
- `babeldoc/format/pdf/high_level.py`: file-task pending propagation and stage memory summaries through the high-level pipeline.
- `file_task_pdf_translate/text_hygiene.py`: editable-source cleanup, paragraph/line/span/font/bbox context serialization, text-role classification, fallback-line figure-label detection, table/protected text filtering, citation and compound-hyphen repair, author/affiliation boundary repair, and placeholder boundary repair before YAML tasks.

For file-backed tasks, `current_translation.yaml` may include read-only metadata such as `context_before` and `text_role`. Validation owns `source` and `translation`; unknown metadata fields are context for the AI and do not render directly. Page-limited runs add previous-page text context to the first lowercase-leading item so cross-page continuations can be translated with the missing preceding words while only the target page is written.

Formula-heavy paragraphs keep rich-text placeholders in file-backed translation tasks, even when the generic BabelDOC LLM path would disable rich text after too many placeholders. The editable/validation loop is the protection layer for placeholder preservation.

`scripts/compare_renders.py` reports changed render pages by numeric page ids parsed from filenames, so `page-3.png` remains page 3 even when filenames would sort lexicographically after `page-29.png`.

Validation invariants:

- Editable task parsing uses YAML with top-level `task` and `items` fields.
- Item count, order, and every `source` field must match the saved snapshot.
- Translation tasks require every `translation` field to be non-empty.
- BabelDOC placeholders remain visible in editable YAML, for example `<b1>` and `</b1>`.
- Required placeholder sequences are derived from each block snapshot by exact `<bN>` scanning.
- Validation compares the saved snapshot sequence with each translation sequence and reports the first mismatch position, expected item, actual item, and local windows.
- Accepted translation answers replay the translation text as written, including required placeholders.
- Every generated page shard is text-scanned for visible `<bN>`, `</bN>`, `{{FORMULA_N}}`, and `{{PROTECTED_N}}` markers before it is committed.

Per-page PDF generation is owned by the internal pipeline: `high_level.translate()` runs layout parsing, paragraph finding, styles/formulas, IL translation, typesetting, font mapping, and PDF creation for the isolated active page.

Generated PDFs are clean by default. The bundled pipeline omits the visible BabelDOC first-page watermark path, while BabelDOC metadata attribution remains in the PDF producer metadata.

After font subsetting, the runtime rewrites embedded Identity-H TrueType `ToUnicode` CMaps with valid UTF-16BE destinations and CJK compatibility normalization so copied Chinese text remains usable in mainstream readers.

The PDF preprocessing path preserves page annotations and links. Normalization helpers may rewrite streams and xrefs, but `/Annots` entries remain attached to their pages.

Runtime assets are prepared outside translation:

- `scripts/download_assets.py <asset-dir>` downloads the DocLayout ONNX model, fonts, CMap files, and tiktoken cache into the configured directory.
- `manifest.json` records the expected asset names and SHA3-256 hashes.
- If `<asset-dir>` already validates against `manifest.json`, `download_assets.py` returns without contacting upstreams. Individual model, font, CMap, and tiktoken groups also skip upstream selection when every expected file already matches its hash.
- Before every pipeline-bearing advance, `advance` activates the frozen `asset_dir` and validates `manifest.json` plus every required file. Pending-edit responses and output-only assembly steps skip asset loading.
- After `asset_dir` is active, the BabelDOC-derived asset loader reads local files and raises `AssetError` for missing or damaged assets.
