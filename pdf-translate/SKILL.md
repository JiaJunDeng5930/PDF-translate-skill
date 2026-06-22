---
name: pdf-translate
description: Use this skill to translate local academic or technical PDFs with a file-backed advance loop. It applies when a user asks to translate a PDF while preserving layout, formulas, figures, tables, metadata, and PDF structure, and when Codex should prepare a PDF translation config before running an advance loop that edits only current_translation.yaml.
---

# PDF Translate

## Prepare

Prepare runtime assets before translating:

```text
python "<skill-dir>/scripts/download_assets.py" "<asset-dir>"
```

Windows PowerShell:

```powershell
python "<skill-dir>/scripts/download_assets.py" "<asset-dir>"
```

Unix shell:

```sh
python "<skill-dir>/scripts/download_assets.py" "<asset-dir>"
```

Create `pdf_translate.yaml` in the PDF workspace before the first run. Include the source PDF, languages, asset directory, output mode, and pipeline options. Read `references/config.md` for the full schema.

Minimal example:

```yaml
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
asset_dir: "pdf-translate-assets"
output_mode: "mono"
primary_font_family: null
add_formula_placehold_hint: true
```

## Advance Loop

Run the bundled no-argument script from the configured PDF workspace:

```powershell
python "<skill-dir>/scripts/advance.py"
```

Follow the returned `status`:

- `config_error`: fix `pdf_translate.yaml`, then run advance again.
- `asset_error`: prepare the configured `asset_dir` with `scripts/download_assets.py`, then run advance again.
- `needs_ai_edit`: open `editable_file`, edit the YAML fields, save, then run advance again.
- `needs_ai_fix`: correct the same file according to `validation_errors`, save, then run advance again.
- `page_completed`: one page shard was merged into `output_pdfs`; run advance again to start `next_page`.
- `done`: deliver `output_pdf` and use `output_pdfs` when multiple variants were generated.
- `error`: inspect `validation_errors` and the trace tail.

After a pending task exists, the AI-editable surface is `current_translation.yaml`. Program-owned state lives under `.pdf_translate/`.

## Editable File Rules

The editable file is YAML. Keep every `source` field unchanged. Preserve placeholders such as `<b1>` and `</b1>` exactly, in the same order. These placeholders represent formulas, superscripts, affiliation numbers, protected content, or PDF layout fragments.

For translation tasks, write the translation in the configured `lang_out`.

```yaml
task: translate
target_language: zh-CN
items:
  - id: 1
    source: |-
      source text
    translation: |-
      translated text
```

## Runtime

The skill owns the workflow contract: `advance`, config freezing, state, trace, clean editable files, answer validation, accepted-answer replay, and output reporting.

The code under `scripts/babeldoc/` is the internal PDF pipeline. It is derived from BabelDOC and handles PDF parsing, layout analysis, paragraph finding, formula/style protection, typesetting, font mapping, local asset loading, and PDF creation.

If Python imports fail, install the runtime dependencies from `scripts/requirements.txt` into the active environment, then rerun advance.

Read `references/runtime.md` when changing runtime behavior. Read `references/babeldoc-upstream.md` when changing the BabelDOC-derived pipeline source or license attribution.
