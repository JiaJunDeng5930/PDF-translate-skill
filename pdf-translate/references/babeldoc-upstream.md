# BabelDOC-Derived Pipeline

This skill owns the PDF translation workflow. The code under `scripts/babeldoc/` is the internal PDF pipeline derived from BabelDOC source.

- Upstream: https://github.com/funstory-ai/BabelDOC
- Upstream commit used for this copy: `980fd2821d54cbabd270349fe509e8177c35e4c3`
- Upstream license: AGPL-3.0
- Project license: AGPL-3.0-or-later
- License copy: `assets/BABELDOC_LICENSE.txt`
- Third-party notices: `../../THIRD_PARTY_LICENSES.md`

Pipeline scope:

- Preserve PDF parsing, layout analysis, formula/style handling, typesetting, font mapping, asset loading, and PDF generation.
- Add a file-backed AI task boundary around term extraction and LLM translation tasks.
- Replace online LLM calls with `current_translation.txt` tasks that the AI edits between `advance` calls.
- Remove external product entrypoints from the delivered skill surface; `scripts/advance.py` is the execution entrypoint.

When refreshing the source, keep the public skill interface stable: `scripts/advance.py` remains the no-argument entry point, and AI editing remains limited to `current_translation.txt`.
