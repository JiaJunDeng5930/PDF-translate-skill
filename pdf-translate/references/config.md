# Workspace Config

`pdf_translate.yaml` is the translation task contract. Create it in the PDF workspace before the first `advance` call.

## Schema

```yaml
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
asset_dir: "pdf-translate-assets"
pages: null
pages_per_advance: 1
output_mode: "mono"
primary_font_family: null
add_formula_placehold_hint: true
```

Fields:

- `input_pdf`: source PDF path. Relative paths resolve from the workspace root.
- `lang_in`: source language code or name.
- `lang_out`: target language code or name.
- `asset_dir`: prepared runtime asset directory. Relative paths resolve from the workspace root. Create it with `scripts/download_assets.py` before translating.
- `pages`: optional pages expression, such as `"1-3,8"`. It selects pages to
  translate. The output PDF keeps the source document page count; pages outside
  the range are preserved from the source PDF.
- `pages_per_advance`: positive integer, default `1`. One foreground `advance`
  translates or assembles at most this many consecutive selected pages. A page
  range discontinuity starts a new batch. Time and peak memory scale with this
  configured batch size while remaining independent of total document length.
- `output_mode`: `mono`, `dual`, or `both`.
- `primary_font_family`: `null`, `serif`, `sans-serif`, or `script`.
- `add_formula_placehold_hint`: asks the pipeline to include formula placeholder hints in translation prompts.

## State Rule

The first initialized `advance` freezes the task config snapshot and hash into `.pdf_translate/state.json`. Later `advance` calls use that frozen snapshot. If `pdf_translate.yaml` changes after initialization, `advance` returns `config_error`; restore the original file or start a fresh workspace state.

Asset failures return `asset_error`. Fix the configured `asset_dir` by running `scripts/download_assets.py`, then rerun `advance`.
