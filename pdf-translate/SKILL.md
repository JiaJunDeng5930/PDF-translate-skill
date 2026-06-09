---
name: pdf-translate
description: Use this skill to translate local academic or technical PDFs with a file-backed advance loop. It applies when a user asks to translate a PDF while preserving layout, formulas, figures, tables, metadata, and PDF structure, and when Codex should prepare a PDF translation config before running an advance loop that edits only current_translation.txt.
---

# PDF Translate

## Prepare

Prepare runtime assets before translating:

```text
python "<skill-dir>\scripts\download_assets.py" "<asset-dir>"
```

Create `pdf_translate.yaml` in the PDF workspace before the first run. Include the source PDF, languages, asset directory, output mode, watermark mode, and pipeline options. Read `references/config.md` for the full schema.

Minimal example:

```yaml
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
asset_dir: "pdf-translate-assets"
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
- `asset_error`: prepare the configured `asset_dir` with `scripts/download_assets.py`, then run advance again.
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
source term -> target-language term
```

Write `[]` in a term extraction translation block when the source block has no terms.
Source terms are matched against normalized PDF text, so normal words may omit
PDF line-break hyphenation such as `distribu- tions` and `real- time`.

## Runtime

The skill owns the workflow contract: `advance`, config freezing, state, trace, clean editable files, answer validation, accepted-answer replay, and output reporting.

The code under `scripts/babeldoc/` is the internal PDF pipeline. It is derived from BabelDOC and handles PDF parsing, layout analysis, paragraph finding, formula/style protection, typesetting, font mapping, local asset loading, and PDF creation.

If Python imports fail, install the runtime dependencies from `scripts/requirements.txt` into the active environment, then rerun advance.

Read `references/runtime.md` when changing runtime behavior. Read `references/babeldoc-upstream.md` when changing the BabelDOC-derived pipeline source or license attribution.
