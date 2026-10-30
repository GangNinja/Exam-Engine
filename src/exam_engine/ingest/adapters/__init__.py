"""Built-in adapter registration.

Importing this package registers every built-in format. Extra formats can be
added at runtime with :func:`exam_engine.ingest.base.register_adapter`.
"""

from __future__ import annotations

from exam_engine.ingest.adapters.bank_adapter import QuestionBankAdapter
from exam_engine.ingest.adapters.docx_adapter import DocxAdapter
from exam_engine.ingest.adapters.image_adapter import ImageAdapter
from exam_engine.ingest.adapters.pdf_adapter import PdfAdapter
from exam_engine.ingest.adapters.text_adapter import TextAdapter
from exam_engine.ingest.base import DocumentAdapter, register_adapter


def builtin_adapters() -> tuple[DocumentAdapter, ...]:
    adapters: list[DocumentAdapter] = [
        PdfAdapter(),
        DocxAdapter(),
        TextAdapter(),
        QuestionBankAdapter(),
        ImageAdapter(),
    ]
    return tuple(adapters)


def register_builtin_adapters(*, replace: bool = True) -> None:
    for adapter in builtin_adapters():
        register_adapter(adapter, replace=replace)


register_builtin_adapters()

__all__ = [
    "DocxAdapter",
    "ImageAdapter",
    "PdfAdapter",
    "QuestionBankAdapter",
    "TextAdapter",
    "builtin_adapters",
    "register_builtin_adapters",
]
