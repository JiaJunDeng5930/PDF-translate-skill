---
name: pdf-translate
description: Translate local academic or technical PDFs while preserving layout and PDF structure through a file-backed advance loop; use when the user provides a PDF for translation.
---

# PDF Translate

## Prepare

Install the runtime dependencies in the active Python environment:

```text
python -m pip install -r "<skill-dir>/scripts/requirements.txt"
```

Prepare the runtime assets:

```text
python "<skill-dir>/scripts/download_assets.py" "<asset-dir>"
```

Create `pdf_translate.yaml` in the PDF workspace with the four required fields:

```yaml
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
asset_dir: "pdf-translate-assets"
```

Read `references/config.md` before adding page selection, batch size, output variants, font family, or formula-hint settings.

Preparation is complete when both commands exit successfully and `pdf_translate.yaml` contains all four required fields.

## Advance Loop

Run the bundled no-argument script from the configured PDF workspace:

```powershell
python "<skill-dir>/scripts/advance.py"
```

Invoke `advance.py` directly once in the foreground. Read the JSON response and treat its `instruction` as the authoritative next action. Complete that instruction before the next invocation.

Public statuses are exhaustive:

- `locked`, `config_error`, `asset_error`, `needs_ai_edit`, `needs_ai_fix`, `page_completed`, `error`: complete the returned `instruction`, then invoke `advance.py` once again.
- `done`: deliver `output_pdf` and every reported `output_pdfs` variant.

The loop is complete when `status` is `done` and every reported output path exists.

## Pending Translation

When `instruction` names `current_translation.yaml`, fill every `translation` field in the configured `lang_out`. Keep every `source` field unchanged and preserve placeholders such as `<b1>` and `</b1>` exactly in their original order. Use `context_before` and `text_role` as read-only translation context.

Use the active AI agent as the sole translation engine. Treat `.pdf_translate/` as program-owned state.

The edit is complete when every `translation` is non-empty and all source text and placeholder sequences match the generated file. Save it, then invoke `advance.py` once.

## Maintenance

Read `references/runtime.md` when changing runtime behavior. Read `references/babeldoc-upstream.md` when changing the BabelDOC-derived pipeline source or license attribution.
