from pathlib import Path

import orjson

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.frontend.il_creater_active_support import (
    LazyPassthroughInstruction,
)


def _orjson_default(value):
    if isinstance(value, LazyPassthroughInstruction):
        return value.materialize()
    raise TypeError


class ILJsonConverter:
    def to_json(self, document: il_version_1.Document) -> str:
        return orjson.dumps(
            document,
            option=orjson.OPT_APPEND_NEWLINE
            | orjson.OPT_INDENT_2
            | orjson.OPT_SORT_KEYS,
            default=_orjson_default,
        ).decode()

    def write_json(self, document: il_version_1.Document, path: str):
        with Path(path).open("w", encoding="utf-8") as f:
            f.write(self.to_json(document))
