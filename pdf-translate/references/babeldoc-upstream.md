# BabelDOC Upstream

This skill vendors a modified copy of BabelDOC as its PDF engine.

- Upstream: https://github.com/funstory-ai/BabelDOC
- Upstream commit used for this copy: `980fd2821d54cbabd270349fe509e8177c35e4c3`
- Upstream license: AGPL-3.0
- License copy: `assets/BABELDOC_LICENSE.txt`

Modification scope:

- Add a file-backed AI task boundary around BabelDOC LLM tasks.
- Preserve BabelDOC PDF parsing, layout analysis, formula/style handling, typesetting, font mapping, and PDF generation.
- Replace online LLM calls with `current_translation.txt` tasks that the AI edits between `advance` calls.

When refreshing from upstream, keep the public skill interface stable: `scripts/advance.py` remains the no-argument entry point, and AI editing remains limited to `current_translation.txt`.
