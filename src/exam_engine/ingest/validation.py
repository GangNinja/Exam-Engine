"""Input validation: supported types, size limits, path containment, magic bytes."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from exam_engine.config import IngestionLimits
from exam_engine.models.enums import SourceFormat

FORMAT_BY_SUFFIX: dict[str, SourceFormat] = {
    ".pdf": SourceFormat.PDF,
    ".docx": SourceFormat.DOCX,
    ".txt": SourceFormat.TXT,
    ".text": SourceFormat.TXT,
    ".md": SourceFormat.TXT,
    ".csv": SourceFormat.CSV,
    ".json": SourceFormat.JSON,
    ".png": SourceFormat.IMAGE,
    ".jpg": SourceFormat.IMAGE,
    ".jpeg": SourceFormat.IMAGE,
    ".tif": SourceFormat.IMAGE,
    ".tiff": SourceFormat.IMAGE,
    ".bmp": SourceFormat.IMAGE,
    ".webp": SourceFormat.IMAGE,
}

SUPPORTED_SUFFIXES = tuple(sorted(FORMAT_BY_SUFFIX))


class FileValidationError(Exception):
    """A rejected input file, with a stable error code."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class FileFacts:
    path: Path
    suffix: str
    format: SourceFormat
    byte_size: int


def resolve_source_path(
    raw: str | Path,
    base_dir: Path,
    *,
    allow_external: bool = False,
) -> Path:
    """Resolve a manifest path, refusing paths that escape the dataset root.

    Absolute paths are only accepted when ``allow_external`` is explicitly set
    (an operator decision), because manifests are data and cannot be trusted to
    point anywhere on the filesystem.
    """
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (base_dir / candidate).resolve()
    if not allow_external:
        try:
            resolved.relative_to(base_dir.resolve())
        except ValueError as exc:
            raise FileValidationError(
                "path_outside_root",
                f"'{raw}' resolves outside the permitted dataset root '{base_dir}'",
                {"requested": str(raw), "base_dir": str(base_dir)},
            ) from exc
    return resolved


def sniff_format(path: Path) -> SourceFormat | None:
    """Detect the real container type from the first bytes of the file."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        return SourceFormat.PDF
    if head.startswith(b"PK\x03\x04"):
        if _is_docx_container(path):
            return SourceFormat.DOCX
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return SourceFormat.IMAGE
    if head.startswith(b"\xff\xd8\xff"):
        return SourceFormat.IMAGE
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return SourceFormat.IMAGE
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return SourceFormat.IMAGE
    if head.startswith(b"BM"):
        return SourceFormat.IMAGE
    return None


def _is_docx_container(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return "word/document.xml" in names and "[Content_Types].xml" in names


def _looks_binary(head: bytes) -> bool:
    if not head:
        return False
    if b"\x00" in head:
        return True
    # High proportion of non-text control bytes indicates a binary payload.
    control = sum(1 for byte in head if byte < 9 or (13 < byte < 32))
    return control > max(8, len(head) // 20)


def validate_source_file(
    path: Path,
    limits: IngestionLimits | None = None,
) -> FileFacts:
    """Validate an already-resolved path and return facts about it."""
    limits = limits or IngestionLimits()
    if not path.exists():
        raise FileValidationError("file_not_found", f"source file not found: {path}")
    if not path.is_file():
        raise FileValidationError("not_a_file", f"source path is not a file: {path}")

    suffix = path.suffix.lower()
    source_format = FORMAT_BY_SUFFIX.get(suffix)
    if source_format is None:
        raise FileValidationError(
            "unsupported_format",
            f"unsupported file type '{suffix or '(none)'}'; supported: "
            + ", ".join(SUPPORTED_SUFFIXES),
            {"suffix": suffix},
        )

    byte_size = path.stat().st_size
    if byte_size == 0:
        raise FileValidationError("empty_file", f"source file is empty: {path}")
    if byte_size > limits.max_file_bytes:
        raise FileValidationError(
            "file_too_large",
            f"file is {byte_size} bytes, limit is {limits.max_file_bytes} bytes",
            {"byte_size": byte_size, "limit": limits.max_file_bytes},
        )

    sniffed = sniff_format(path)
    if source_format is SourceFormat.PDF and sniffed is not SourceFormat.PDF:
        raise FileValidationError(
            "format_mismatch", f"'{path.name}' has a .pdf suffix but no PDF header"
        )
    if source_format is SourceFormat.DOCX and sniffed is not SourceFormat.DOCX:
        raise FileValidationError(
            "format_mismatch",
            f"'{path.name}' is not a valid DOCX container (expected OOXML zip)",
        )
    if source_format is SourceFormat.IMAGE and sniffed is not SourceFormat.IMAGE:
        raise FileValidationError(
            "format_mismatch", f"'{path.name}' has an image suffix but no image header"
        )
    if source_format in (SourceFormat.TXT, SourceFormat.CSV, SourceFormat.JSON):
        with path.open("rb") as handle:
            head = handle.read(4096)
        if _looks_binary(head):
            raise FileValidationError(
                "binary_content",
                f"'{path.name}' is declared as text but contains binary content",
            )

    return FileFacts(path=path, suffix=suffix, format=source_format, byte_size=byte_size)


def read_limited_bytes(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes, guarding against unexpected growth."""
    with path.open("rb") as handle:
        return handle.read(limit)
