# Third-Party Licenses

This repository is licensed as `AGPL-3.0-or-later`; see `LICENSE`.

## Bundled Source

### BabelDOC-derived PDF pipeline

- Path: `pdf-translate/scripts/babeldoc/`
- Upstream: https://github.com/funstory-ai/BabelDOC
- Upstream commit: `980fd2821d54cbabd270349fe509e8177c35e4c3`
- License: `AGPL-3.0-or-later`
- License text: `pdf-translate/assets/BABELDOC_LICENSE.txt`
- Local changes: file-backed AI task pause/resume boundaries, removal of external product entrypoints from the delivered skill surface, and project packaging/documentation.

### pdfminer code bundled inside BabelDOC

- Path: `pdf-translate/scripts/babeldoc/pdfminer/`
- Copyright: `Copyright (c) 2004-2016 Yusuke Shinyama`
- License: MIT
- License text: `pdf-translate/scripts/babeldoc/pdfminer/LICENSE`

## Runtime Dependencies

Python dependencies are declared in `pdf-translate/scripts/requirements.txt` and are not redistributed in this repository or the skill zip. Installers should resolve and review their package metadata from the installed distributions.
