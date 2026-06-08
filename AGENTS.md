# Project Notes

This repository contains a Codex skill at `pdf-translate/`.

Important invariants:

- The skill public interface is the no-argument `pdf-translate/scripts/advance.py` script.
- The target PDF workspace is the current working directory and must contain exactly one source PDF on first run.
- AI-facing editing is limited to `current_translation.txt`.
- Program-owned state stays under `.pdf_translate/`.
- BabelDOC is vendored under `pdf-translate/scripts/babeldoc/`; offline workflow patches belong at the term extraction and LLM translation batch boundaries.
- Keep BabelDOC license attribution in `pdf-translate/references/babeldoc-upstream.md` and `pdf-translate/assets/BABELDOC_LICENSE.txt`.

Before changing runtime behavior, read `pdf-translate/references/runtime.md`.
