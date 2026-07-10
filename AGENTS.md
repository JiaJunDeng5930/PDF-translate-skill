# Project Notes

This repository contains a Codex skill at `pdf-translate/`.

Important invariants:

- The skill public interface is the no-argument `pdf-translate/scripts/advance.py` script.
- The target PDF workspace is the current working directory and must contain `pdf_translate.yaml` before first run.
- `pdf_translate.yaml` must include `asset_dir`; prepare that directory with `pdf-translate/scripts/download_assets.py` before translation.
- AI may write `pdf_translate.yaml` during preparation; pending-task editing is limited to `current_translation.yaml`.
- Program-owned state stays under `.pdf_translate/`.
- Each `advance` processes at most one selected or output page; completed public PDFs appear only at `done`.
- `pdf-translate/scripts/babeldoc/` contains BabelDOC-derived internal PDF pipeline code; file-task patches belong at the page translation boundary.
- Keep BabelDOC license attribution in `pdf-translate/references/babeldoc-upstream.md` and `pdf-translate/assets/BABELDOC_LICENSE.txt`.

Before changing runtime behavior, read `pdf-translate/references/runtime.md`.
