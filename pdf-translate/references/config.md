# Workspace Config

`pdf_translate.yaml` is the translation task contract. Create it in the PDF workspace before the first `advance` call.

## Schema

```yaml
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
asset_dir: "pdf-translate-assets"
pages: null
output_mode: "mono"
watermark_output_mode: "watermarked"
auto_extract_glossary: true
primary_font_family: null
add_formula_placehold_hint: true
table_model: null
```

Fields:

- `input_pdf`: source PDF path. Relative paths resolve from the workspace root.
- `lang_in`: source language code or name.
- `lang_out`: target language code or name.
- `asset_dir`: prepared runtime asset directory. Relative paths resolve from the workspace root. Create it with `scripts/download_assets.py` before translating.
- `pages`: optional pages expression, such as `"1-3,8"`. It selects pages to
  translate. The output PDF keeps the source document page count; pages outside
  the range are preserved from the source PDF.
- `output_mode`: `mono`, `dual`, or `both`.
- `watermark_output_mode`: `watermarked`, `no_watermark`, or `both`.
- `auto_extract_glossary`: enables the LLM glossary pre-pass.
- `primary_font_family`: `null`, `serif`, `sans-serif`, or `script`.
- `add_formula_placehold_hint`: asks the pipeline to include formula placeholder hints in translation prompts.
- `table_model`: `null` or `rapidocr`. `rapidocr` enables the table parsing stage with the packaged compatibility model.

## State Rule

The first initialized `advance` freezes the task config snapshot and hash into `.pdf_translate/state.json`. Later `advance` calls use that frozen snapshot. If `pdf_translate.yaml` changes after initialization, `advance` returns `config_error`; restore the original file or start a fresh workspace state.

Asset failures return `asset_error`. Fix the configured `asset_dir` by running `scripts/download_assets.py`, then rerun `advance`.
