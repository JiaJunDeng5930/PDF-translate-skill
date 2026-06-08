# Runtime Notes

The public interface is `scripts/advance.py` with no arguments. It uses the current working directory as the PDF workspace and requires exactly one source PDF on first run.

State model:

- `current_translation.txt`: the only AI-editable file.
- `.pdf_translate/state.json`: source PDF path, pending task, accepted answer index, final output.
- `.pdf_translate/tasks/`: snapshots for pending tasks.
- `.pdf_translate/accepted_answers/`: accepted editable files and JSON answers replayed into BabelDOC.
- `.pdf_translate/rejected_answers/`: damaged editable files archived before template restoration.
- `.pdf_translate/trace.jsonl`: compact progress and validation events.

The runtime re-enters BabelDOC from the beginning on each advance and replays accepted answers by stable task hash. This keeps the workflow resumable while avoiding AI-visible checkpoints. The deterministic replay boundary is the program-owned checkpoint model.

The modified BabelDOC files are:

- `babeldoc/offline_bridge.py`: pending exception and immediate executor.
- `babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py`: offline sequential term extraction and pending propagation.
- `babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py`: offline sequential translation batches and pending propagation.

Validation invariants:

- Text block parsing is line-state based, so empty translation blocks remain valid blocks.
- Source block count, order, and content must match the saved snapshot.
- Translation tasks require every translation block to be non-empty.
- Protected marker sequence must match the source block sequence.
- Term extraction pairs must parse as `source → target`, and the source term must occur in the matching source block.

PDF generation remains BabelDOC-owned: `high_level.translate()` runs layout parsing, paragraph finding, styles/formulas, term extraction, IL translation, typesetting, and PDF creation.
