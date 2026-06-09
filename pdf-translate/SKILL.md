---
name: pdf-translate
description: Use this skill to translate local academic or technical PDFs with a file-backed BabelDOC task workflow. It applies when a user asks to translate a PDF while preserving layout, formulas, figures, tables, metadata, and PDF structure, and when Codex should prepare a PDF translation config before running an advance loop that edits only current_translation.txt.
---

# PDF Translate

## Prepare

Create `pdf_translate.yaml` in the PDF workspace before the first run. Include the source PDF, languages, output mode, watermark mode, and BabelDOC options. Read `references/config.md` for the full schema.

Minimal example:

```yaml
version: 1
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
output_mode: "mono"
watermark_output_mode: "watermarked"
auto_extract_glossary: true
primary_font_family: null
add_formula_placehold_hint: true
```

## Advance Loop

Run the bundled no-argument script from the configured PDF workspace:

```powershell
python "<skill-dir>\scripts\advance.py"
```

Follow the returned `status`:

- `config_error`: fix `pdf_translate.yaml`, then run advance again.
- `needs_ai_edit`: open `editable_file`, fill every `⟦TRANSLATION⟧` section, save, then run advance again.
- `needs_ai_fix`: correct the same file according to `validation_errors`, save, then run advance again.
- `done`: deliver `output_pdf` and use `output_pdfs` when multiple variants were generated.
- `error`: inspect `validation_errors` and the trace tail.

After a pending task exists, the AI-editable surface is `current_translation.txt`. Program-owned state lives under `.pdf_translate/`.

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

For translation tasks, write the translation in the configured `lang_out`.

For term extraction tasks, write one term pair per line:

```text
source term ? target-language term
```

Write `[]` in a term extraction translation block when the source block has no terms.

## Runtime

The runtime uses the vendored modified BabelDOC engine under `scripts/babeldoc/`. BabelDOC handles PDF parsing, layout analysis, paragraph finding, formula/style protection, typesetting, font mapping, and PDF creation. The skill runtime handles `advance`, config freezing, state, trace, clean editable files, answer validation, and replay of accepted AI answers.

If Python imports fail, install the runtime dependencies from `scripts/requirements.txt` into the active environment, then rerun advance.

Read `references/runtime.md` when changing runtime behavior. Read `references/babeldoc-upstream.md` when updating the vendored BabelDOC source or license attribution.
