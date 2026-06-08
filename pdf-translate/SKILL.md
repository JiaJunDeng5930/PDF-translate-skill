---
name: pdf-translate
description: Use this skill to translate local academic or technical PDFs into Simplified Chinese with a file-backed offline BabelDOC workflow. It applies when a user asks to translate a PDF while preserving layout, formulas, figures, tables, links, metadata, and PDF structure, and when the AI should edit a clean current_translation.txt file instead of calling an online LLM API.
---

# PDF Translate

## Core Workflow

Run the bundled no-argument advance script from a workspace that contains exactly one source PDF:

```powershell
python "<skill-dir>\scripts\advance.py"
```

The script returns JSON. Follow the returned `status`:

- `needs_ai_edit`: open `editable_file`, fill every `⟦TRANSLATION⟧` section, save, then run advance again.
- `needs_ai_fix`: correct the same file according to `validation_errors`, save, then run advance again.
- `done`: deliver `output_pdf`.
- `error`: inspect `validation_errors` and the trace tail.

The only AI-editable file is the returned `current_translation.txt`. Program-owned state lives under `.pdf_translate/`.

## Editable File Rules

Each block has this shape:

```text
⟦SOURCE⟧
source text
⟦TRANSLATION⟧
translated text or term pairs
⟦END⟧
```

Keep every source block unchanged. Fill translation blocks in order. Preserve protected markers such as `⟦FORMULA⟧`, `⟦INLINE_MATH⟧`, and `⟦PROTECTED_TEXT⟧` in the same order.

For translation tasks, write only Simplified Chinese translation in each translation block.

For term extraction tasks, write one term pair per line:

```text
source term → Chinese term
```

Write `[]` in a term extraction translation block when the source block has no terms.

## Runtime

The runtime uses the vendored modified BabelDOC engine under `scripts/babeldoc/`. BabelDOC handles PDF parsing, layout analysis, paragraph finding, formula/style protection, typesetting, font mapping, and PDF creation. The skill runtime handles `advance`, state, trace, clean editable files, answer validation, and replay of accepted AI answers.

If Python imports fail, install the runtime dependencies from `scripts/requirements.txt` into the active environment, then rerun advance.

Read `references/runtime.md` when changing the runtime behavior. Read `references/babeldoc-upstream.md` when updating the vendored BabelDOC source or license attribution.
