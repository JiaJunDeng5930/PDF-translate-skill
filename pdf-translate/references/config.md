# Workspace Config

`pdf_translate.yaml` is the preparation-stage contract. Create it in the PDF workspace before the first `advance` call.

## Schema

```yaml
version: 1
input_pdf: "paper.pdf"
lang_in: "en"
lang_out: "zh-CN"
pages: null
output_mode: "mono"
watermark_output_mode: "watermarked"
auto_extract_glossary: true
primary_font_family: null
add_formula_placehold_hint: true
```

Fields:

- `version`: must be `1`.
- `input_pdf`: source PDF path. Relative paths resolve from the workspace root.
- `lang_in`: BabelDOC source language code or name.
- `lang_out`: BabelDOC target language code or name.
- `pages`: optional BabelDOC pages expression, such as `"1-3,8"`.
- `output_mode`: `mono`, `dual`, or `both`.
- `watermark_output_mode`: `watermarked`, `no_watermark`, or `both`.
- `auto_extract_glossary`: enables BabelDOC LLM glossary pre-pass.
- `primary_font_family`: `null`, `serif`, `sans-serif`, or `script`.
- `add_formula_placehold_hint`: asks BabelDOC to include formula placeholder hints in translation prompts.

## State Rule

The first successful `advance` freezes the config snapshot and hash into `.pdf_translate/state.json`. Later `advance` calls use that frozen snapshot. If `pdf_translate.yaml` changes after initialization, `advance` returns `config_error`; restore the original file or start a fresh workspace state.
