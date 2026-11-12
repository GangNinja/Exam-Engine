"""Bounded public-document retrieval, extraction and persistent caching.

Search snippets are discovery leads only.  This module retrieves a small number
of explicitly linked public documents, extracts text with PyMuPDF or the standard
HTML parser, and records a content fingerprint and extraction method.  It never
follows authenticated, private or loopback URLs, and it does not attempt to bypass
access controls.

The cache is stored in the local recovery database.  A successful retrieval is
reused across runs; failed retrievals are cached with an error and can be retried
with ``retry_failed=True``.
"""

from __future__ import annotations

import hashlib
import html
import io
import ipaddress
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Protocol

from exam_engine.models.recovery import SourceCandidate, now_iso
from exam_engine.storage.recovery_repository import RecoveryRepository

logger = logging.getLogger(__name__)

_MAX_BYTES = 6_000_000
_MAX_TEXT = 2_000_000
_DOCUMENT_TYPES = {
    "question_paper",
    "answer_key",
    "official_solution",
    "marking_scheme",
    "solved_paper",
}
_DOCUMENT_SUFFIXES = (".pdf", ".zip", ".csv", ".txt", ".docx")
_DOCUMENT_TERMS = (
    "question-paper",
    "question_paper",
    "questionpaper",
    "answer-key",
    "answer_key",
    "answerkey",
    "master-question",
    "solution",
    "marking-scheme",
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value or ""))).strip()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only public http(s) documents can be retrieved")
    host = parsed.hostname or ""
    if host.casefold() in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("local document hosts are not permitted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    ):
        raise ValueError("private/loopback document hosts are not permitted")
    return url


class _HTMLText(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template"}
    _BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "section",
        "article",
        "table",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "a":
            values = {key.casefold(): value or "" for key, value in attrs}
            if values.get("href"):
                self._link_href = values["href"]
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._link_href:
            label = " ".join("".join(self._link_text).split())
            self.links.append((self._link_href, label))
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = " ".join((data or "").split())
        if not value:
            return
        self.parts.append(value + " ")
        if self._link_href:
            self._link_text.append(value)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(payload: str, base_url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    """Return ``(title, text, links)`` from an HTML page."""

    title_match = _TITLE_RE.search(payload or "")
    title = _clean(title_match.group(1)) if title_match else ""
    parser = _HTMLText()
    try:
        parser.feed(payload or "")
    except Exception:  # noqa: BLE001 - malformed public HTML is tolerated
        pass
    links = [
        (urllib.parse.urljoin(base_url, href), label)
        for href, label in parser.links
        if href and not href.startswith(("javascript:", "mailto:", "#"))
    ]
    return title, parser.text()[: _MAX_TEXT], links


def is_document_url(url: str, label: str = "") -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.casefold()
    if path.endswith(_DOCUMENT_SUFFIXES):
        return True
    combined = f"{path} {label}".casefold()
    return any(term in combined for term in _DOCUMENT_TERMS)


def extract_document_links(
    page_html: str, base_url: str, *, limit: int = 4
) -> list[tuple[str, str]]:
    """Extract public document links from a retrieved HTML page."""

    _title, _text, links = html_to_text(page_html, base_url)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, label in links:
        if url in seen or not is_document_url(url, label):
            continue
        try:
            _safe_url(url)
        except ValueError:
            continue
        seen.add(url)
        found.append((url, label))
        if len(found) >= limit:
            break
    return found


@dataclass
class FetchedDocument:
    url: str
    final_url: str
    title: str
    content_type: str
    text: str
    sha256: str
    bytes_read: int
    page_count: int | None = None
    extraction_method: str = "unknown"
    links: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retrieved_at: str = field(default_factory=now_iso)
    http_status: int = 200
    markdown: str = ""
    tables: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    canonical_url: str = ""
    crawler: str = "http"
    browser_ms: int | None = None

    @property
    def is_html(self) -> bool:
        return self.content_type in {"text/html", "application/xhtml+xml"}

    def cache_payload(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "content_type": self.content_type,
            "text": self.text[: _MAX_TEXT],
            "text_chars": len(self.text),
            "sha256": self.sha256,
            "bytes_read": self.bytes_read,
            "page_count": self.page_count,
            "extraction_method": self.extraction_method,
            "http_status": self.http_status,
            "warnings": self.warnings,
            "fetched_at": self.retrieved_at,
            "markdown": self.markdown[: _MAX_TEXT],
            "links": [{"url": url, "label": label} for url, label in self.links],
            "tables": self.tables,
            "metadata": self.metadata,
            "canonical_url": self.canonical_url,
            "crawler": self.crawler,
            "browser_ms": self.browser_ms,
            "status": "ok",
            "error": "",
        }


class DocumentFetcher(Protocol):
    def fetch_document(self, url: str) -> FetchedDocument: ...


class DocumentFetcher:
    """Small, conservative HTTP(S) fetcher with no privilege escalation."""

    def __init__(
        self,
        *,
        max_bytes: int = _MAX_BYTES,
        timeout: float = 18.0,
        delay_seconds: float = 0.6,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        self.max_bytes = max(64_000, min(int(max_bytes), _MAX_BYTES))
        self.timeout = max(4.0, min(float(timeout), 60.0))
        self.delay_seconds = max(0.0, min(float(delay_seconds), 10.0))
        self.allowed_hosts = tuple(host.casefold() for host in (allowed_hosts or ()))
        self._last = 0.0
        self._lock = __import__("threading").Lock()

    def _throttle(self) -> None:
        with self._lock:
            wait = self.delay_seconds - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _validate(self, url: str) -> str:
        url = _safe_url(url)
        host = (urllib.parse.urlparse(url).hostname or "").casefold()
        if self.allowed_hosts and not any(
            host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts
        ):
            raise ValueError("document host is not in the permitted allowlist")
        return url

    def fetch_document(self, url: str) -> FetchedDocument:
        url = self._validate(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ExamEngine/0.2 (local educational source verification)",
                "Accept": "application/pdf,text/html,text/csv,text/plain;q=0.9,*/*;q=0.1",
                "Accept-Language": "en-IN,en;q=0.8",
            },
        )
        self._throttle()
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            final_url = self._validate(response.geturl())
            header_type = str(response.headers.get("Content-Type") or "").split(";")[0].casefold()
            payload = response.read(self.max_bytes + 1)
            status = int(getattr(response, "status", 200) or 200)
        if len(payload) > self.max_bytes:
            raise ValueError("document exceeds the configured retrieval size")
        sha = _sha256(payload)
        warnings: list[str] = []
        title = ""
        links: list[tuple[str, str]] = []
        page_count: int | None = None
        text = ""
        method = "unknown"
        content_type = header_type
        if payload[:4] == b"%PDF" or "pdf" in header_type:
            content_type = "application/pdf"
            page_count, text, method, pdf_warnings = _pdf_text(payload)
            warnings.extend(pdf_warnings)
        elif "html" in header_type or payload.lstrip()[:1] == b"<":
            content_type = "text/html"
            decoded = payload.decode("utf-8", "replace")
            title, text, links = html_to_text(decoded, final_url)
            method = "html_text"
        elif "csv" in header_type or "text/" in header_type or not header_type:
            content_type = header_type or "text/plain"
            text = payload.decode("utf-8", "replace")[: _MAX_TEXT]
            method = "plain_text"
        else:
            raise ValueError(f"unsupported document content type: {header_type or 'unknown'}")
        if not text.strip():
            warnings.append("document produced no extractable text")
        return FetchedDocument(
            url=url,
            final_url=final_url,
            title=title,
            content_type=content_type,
            text=text[: _MAX_TEXT],
            sha256=sha,
            bytes_read=len(payload),
            page_count=page_count,
            extraction_method=method,
            links=links,
            warnings=warnings,
            http_status=status,
        )

    def fetch(self, url: str) -> tuple[str, str]:
        """Compatibility wrapper returning ``(content_type, text)``."""

        document = self.fetch_document(url)
        return document.content_type, document.text


def _pdf_text(
    payload: bytes, *, allow_ocr: bool = True, max_pages: int = 60
) -> tuple[int | None, str, str, list[str]]:
    warnings: list[str] = []
    try:
        import fitz  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, "", "pdf_unavailable", [f"PDF extraction unavailable: {type(exc).__name__}"]
    try:
        document = fitz.open(stream=payload, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        return None, "", "pdf_failed", [f"PDF could not be opened: {type(exc).__name__}"]
    try:
        page_count = int(document.page_count)
        page_limit = min(page_count, max(1, int(max_pages)))
        parts = [
            document.load_page(index).get_text("text")
            for index in range(page_limit)
        ]
        text = "\n".join(parts)
        method = "pymupdf_text"
        if page_count > page_limit:
            warnings.append(
                f"PDF text extraction limited to first {page_limit} of {page_count} pages"
            )
        if len(" ".join(text.split())) < 40:
            if not allow_ocr:
                warnings.append(
                    "PDF text layer is empty; OCR disabled for multi-document ZIP bundle"
                )
            else:
                ocr_text = _ocr_pdf(document)
                if ocr_text.strip():
                    text = ocr_text
                    method = "pymupdf_ocr"
                else:
                    warnings.append("PDF text layer is empty and no OCR provider is available")
        return page_count, text[: _MAX_TEXT], method, warnings
    finally:
        document.close()


def _ocr_pdf(document: Any) -> str:
    try:
        import pytesseract  # type: ignore
    except Exception:  # noqa: BLE001 - OCR is optional
        return ""
    parts: list[str] = []
    try:
        for index in range(min(int(document.page_count), 20)):
            page = document.load_page(index)
            pixmap = page.get_pixmap(dpi=150, alpha=False)
            parts.append(pytesseract.image_to_string(pixmap.tobytes("png")))
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(parts)[: _MAX_TEXT]


class ZipBundleFetcher:
    """Retrieve the English PDF members of a public official ZIP archive.

    CBSE publishes question papers and marking schemes as ZIP bundles.  A URL
    fragment such as ``#set=1`` selects the member whose CBSE set code matches
    the local record's set.  If no set-specific member exists, retrieval fails
    rather than silently substituting a different set or the whole bundle.
    """

    # CBSE question-paper names encode ``subject/region/set`` (for example
    # ``30-1-1``); the final component is the set used by local record IDs.
    # A trailing ``,2,3`` or ``to 3`` means one PDF contains several sets and
    # must be treated as multi-set evidence.
    _QP_CODE_RE = re.compile(
        r"(?<!\d)(\d{2,3})[-_/](\d{1,3})[-_/](\d{1,2}(?:\s*(?:,|to)\s*\d{1,2})*)(?!\d)",
        re.IGNORECASE,
    )
    _SET_LABEL_RE = re.compile(
        r"(?:^|[-_ ])set\s*(?:no\.?\s*)?(\d)(?:[-_ .]|$)", re.IGNORECASE
    )

    def __init__(
        self,
        fetcher: "DocumentFetcher | None" = None,
        *,
        max_zip_bytes: int = 130_000_000,
        timeout: float = 60.0,
    ) -> None:
        self.fetcher = fetcher or DocumentFetcher()
        self.max_zip_bytes = max(1_000_000, min(int(max_zip_bytes), 140_000_000))
        self.timeout = max(5.0, min(float(timeout), 90.0))

    @classmethod
    def _member_code_match(cls, name: str) -> re.Match[str] | None:
        base = Path(name).name
        pattern = re.compile(
            r"(\d{2,3})[-_/](\d{1,3})[-_/](\d{1,2}(?:\s*(?:,|to)\s*\d{1,2})*)(?!\d)",
            re.IGNORECASE,
        )
        matches: list[re.Match[str]] = []
        # Search every start offset so codes embedded after a longer prefix
        # (``MS 041_30-1-1``) still yield the final ``30-1-1`` code.
        for start in range(len(base)):
            if start and base[start - 1].isdigit():
                continue
            match = pattern.match(base, start)
            if match:
                matches.append(match)
        return matches[-1] if matches else None

    @classmethod
    def member_code(cls, name: str) -> str:
        match = cls._member_code_match(name)
        if not match:
            return ""
        set_value = re.match(r"\d+", match.group(3))
        return f"{match.group(1)}/{match.group(2)}/{set_value.group(0) if set_value else ''}"

    @classmethod
    def member_region(cls, name: str) -> str:
        match = cls._member_code_match(name)
        return str(int(match.group(2))) if match else ""

    @classmethod
    def member_sets(cls, name: str) -> set[str]:
        base = Path(name).name
        match = cls._member_code_match(base)
        if match:
            third = match.group(3)
            values = {str(int(value)) for value in re.findall(r"\d+", third)}
            range_match = re.search(r"\bto\s*(\d+)", third, re.IGNORECASE)
            if range_match:
                values.update(
                    str(value) for value in range(1, int(range_match.group(1)) + 1)
                )
            trailing_range = re.search(r"\bto\s*(\d+)", base[base.find(third) + len(third) :], re.I)
            if trailing_range:
                values.update(
                    str(value)
                    for value in range(1, int(trailing_range.group(1)) + 1)
                )
            if values:
                return values
        label = cls._SET_LABEL_RE.search(base)
        return {str(int(label.group(1)))} if label else set()

    @classmethod
    def member_set(cls, name: str) -> str:
        values = cls.member_sets(name)
        return min(values, key=int) if values else ""

    @staticmethod
    def _english_members(members: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
        english = [
            item
            for item in members
            if "hindi" not in item.filename.casefold()
            or "english" in item.filename.casefold()
        ]
        return english or members

    def fetch_document(self, url: str) -> FetchedDocument:
        base_url, _, fragment = url.partition("#")
        base_url = _safe_url(base_url)
        preferred = ""
        for part in fragment.split("&"):
            key, _, value = part.partition("=")
            if key == "set":
                preferred = value.strip()
        request = urllib.request.Request(
            base_url,
            headers={
                "User-Agent": "ExamEngine/0.2 (local educational source verification)",
                "Accept": "application/zip,application/x-zip-compressed,*/*;q=0.5",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            final_url = _safe_url(response.geturl())
            payload = response.read(self.max_zip_bytes + 1)
        if len(payload) > self.max_zip_bytes:
            raise ValueError("ZIP archive exceeds the configured retrieval size")
        if payload[:2] != b"PK":
            raise ValueError("official bundle did not return ZIP bytes")
        try:
            bundle = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ValueError("official bundle could not be opened as a ZIP archive") from exc
        members = [
            item
            for item in bundle.infolist()
            if not item.is_dir()
            and item.filename.casefold().endswith(".pdf")
            and item.file_size > 0
        ]
        members = self._english_members(members)
        if not members:
            raise ValueError("official ZIP archive contains no English PDF members")
        selected: list[zipfile.ZipInfo] = []
        resolved_set = ""
        ambiguous = False
        if preferred and preferred != "all":
            matching = [
                item for item in members if preferred in self.member_sets(item.filename)
            ]
            if not matching:
                # Never silently substitute a different set or the whole
                # bundle.  A missing requested set is a retrieval failure, not
                # evidence for the local record's set.
                raise ValueError(
                    f"official ZIP archive has no member for requested set {preferred}"
                )
            matching.sort(key=lambda item: item.filename.casefold())
            selected = matching[:1]
            resolved_set = preferred
            ambiguous = len(matching) > 1 or len(
                self.member_sets(selected[0].filename)
            ) > 1
        else:
            selected = sorted(members, key=lambda item: item.filename.casefold())[:12]
            resolved_set = "all"
            ambiguous = len({self.member_set(item.filename) for item in members}) > 1
        parts: list[str] = []
        payloads: list[bytes] = []
        page_count = 0
        warnings: list[str] = []
        methods: set[str] = set()
        selected_code = self.member_code(selected[0].filename) if selected else ""
        selected_region = self.member_region(selected[0].filename) if selected else ""
        selected_sets = (
            sorted(self.member_sets(selected[0].filename), key=int) if selected else []
        )
        for item in selected:
            if item.file_size > 25_000_000:
                warnings.append(f"member skipped as too large: {item.filename}")
                continue
            try:
                data = bundle.read(item)
            except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                warnings.append(f"member unreadable: {item.filename}: {type(exc).__name__}")
                continue
            pages, text, method, member_warnings = _pdf_text(
                data, allow_ocr=False, max_pages=24
            )
            payloads.append(data)
            page_count += pages or 0
            methods.add(method)
            warnings.extend(f"{item.filename}: {message}" for message in member_warnings)
            parts.append(f"\n[zip-member: {item.filename}]\n{text}")
        if not payloads:
            raise ValueError("official ZIP archive members produced no readable PDF")
        text = "\n".join(parts)[: _MAX_TEXT]
        return FetchedDocument(
            url=url,
            final_url=final_url,
            title=selected[0].filename,
            content_type="application/pdf",
            text=text,
            sha256=_sha256(b"\x1f".join(payloads)),
            bytes_read=len(payload),
            page_count=page_count or None,
            extraction_method="zip_pdf_bundle",
            warnings=warnings,
            metadata={
                "zip_url": final_url,
                "zip_set_requested": preferred or None,
                "zip_set_resolved": resolved_set or None,
                "zip_set_ambiguous": ambiguous,
                "zip_selection_version": 2,
                "zip_member_code": selected_code or None,
                "zip_member_region": selected_region or None,
                "zip_member_sets": selected_sets,
                "zip_members": [item.filename for item in selected],
                "zip_member_count": len(selected),
                "pdf_methods": sorted(methods),
            },
            crawler="zip",
        )


class RetrievalCache(Protocol):
    def get(self, url: str) -> dict[str, Any] | None: ...

    def put(self, payload: dict[str, Any]) -> None: ...


class DatabaseRetrievalCache:
    """Persistent cache over the existing recovery database."""

    def __init__(self, database: Any) -> None:
        self.database = database

    def get(self, url: str) -> dict[str, Any] | None:
        try:
            with self.database.session() as session:
                return RecoveryRepository(session).cached_document(url)
        except Exception:  # noqa: BLE001 - cache is optional, never fatal
            logger.info("retrieval cache read unavailable", exc_info=True)
            return None

    def put(self, payload: dict[str, Any]) -> None:
        try:
            with self.database.transaction() as session:
                RecoveryRepository(session).save_cached_document(payload)
        except Exception:  # noqa: BLE001
            logger.info("retrieval cache write unavailable", exc_info=True)


@dataclass
class RetrievedDocument:
    candidate: SourceCandidate
    fetched: FetchedDocument
    cache_hit: bool = False
    followed_from: str | None = None


class SourceRetriever:
    """Retrieve a bounded set of documents for a course's candidates."""

    def __init__(
        self,
        fetcher: DocumentFetcher | None = None,
        *,
        browser_fetcher: Any = None,
        zip_fetcher: Any = None,
        cache: RetrievalCache | None = None,
        retry_failed: bool = False,
    ) -> None:
        self.fetcher = fetcher or DocumentFetcher()
        self.browser_fetcher = browser_fetcher
        self.zip_fetcher = zip_fetcher or ZipBundleFetcher(self.fetcher)
        self.cache = cache
        self.retry_failed = retry_failed

    def _fetcher_for(self, url: str) -> Any:
        path = urllib.parse.urlparse(url.partition("#")[0]).path.casefold()
        if path.endswith(".zip"):
            return self.zip_fetcher
        if path.endswith(_DOCUMENT_SUFFIXES):
            return self.fetcher
        return self.browser_fetcher or self.fetcher

    def _cached(self, url: str) -> tuple[FetchedDocument | None, bool]:
        if not self.cache:
            return None, False
        row = self.cache.get(url)
        if not row:
            return None, False
        if row.get("status") != "ok" and not self.retry_failed:
            return None, False
        if row.get("status") != "ok":
            return None, False
        try:
            fetched = FetchedDocument(
                url=row.get("url") or url,
                final_url=row.get("final_url") or url,
                title=row.get("title") or "",
                content_type=row.get("content_type") or "",
                text=row.get("text") or "",
                sha256=row.get("sha256") or "",
                bytes_read=int(row.get("bytes_read") or 0),
                page_count=row.get("page_count"),
                extraction_method=row.get("extraction_method") or "cache",
                warnings=list(row.get("warnings") or []),
                retrieved_at=row.get("fetched_at") or now_iso(),
                http_status=int(row.get("http_status") or 200),
                markdown=row.get("markdown") or "",
                tables=list(row.get("tables") or []),
                metadata=dict(row.get("metadata") or {}),
                canonical_url=row.get("canonical_url") or "",
                crawler=row.get("crawler") or "http",
                browser_ms=row.get("browser_ms"),
            )
        except (TypeError, ValueError):
            return None, False
        fragment = url.partition("#")[2]
        path = urllib.parse.urlparse(url.partition("#")[0]).path.casefold()
        if path.endswith(".zip") and "set=" in fragment and "set=all" not in fragment:
            # Older cache entries could contain every set because selection
            # fell back to the whole bundle.  They are not safe evidence for a
            # set-specific identity; re-fetch with the current selector.
            if int((fetched.metadata or {}).get("zip_selection_version") or 0) < 2:
                return None, False
        return fetched, True

    def retrieve(
        self,
        candidates: Iterable[SourceCandidate],
        *,
        max_documents: int = 2,
        follow_links: int = 1,
    ) -> list[RetrievedDocument]:
        max_documents = max(0, min(int(max_documents), 8))
        follow_links = max(0, min(int(follow_links), 3))
        if not max_documents:
            return []
        queue = sorted(
            (
                item
                for item in candidates
                if item.document_type in _DOCUMENT_TYPES
                or (
                    item.source_tier == 1
                    and any(
                        term in f"{item.title} {item.snippet}".casefold()
                        for term in ("question", "answer", "solution", "paper")
                    )
                )
            ),
            key=lambda item: (item.source_tier, -item.reliability, item.title.casefold()),
        )
        results: list[RetrievedDocument] = []
        seen_hashes: set[str] = set()
        errors: list[str] = []
        for candidate in queue:
            if len(results) >= max_documents:
                break
            cached, cache_hit = self._cached(candidate.url)
            if cached is not None:
                if cached.sha256 in seen_hashes:
                    continue
                seen_hashes.add(cached.sha256)
                candidate.retrieval_status = "cached"
                candidate.evidence.update(self._evidence(cached, cache_hit=True, error=""))
                results.append(
                    RetrievedDocument(candidate=candidate, fetched=cached, cache_hit=True)
                )
                continue
            try:
                selected_fetcher = self._fetcher_for(candidate.url)
                fetched = selected_fetcher.fetch_document(candidate.url)
            except Exception as exc:  # noqa: BLE001 - isolate one failed source
                message = f"{type(exc).__name__}: {exc}"[:500]
                errors.append(f"{candidate.url}: {message}")
                candidate.retrieval_status = "blocked" if getattr(exc, "kind", "") == "blocked" else "failed"
                candidate.evidence.update(
                    {
                        "retrieval_http_status": getattr(exc, "status_code", None),
                        "retrieval_error": message,
                        "retrieval_failure_kind": getattr(exc, "kind", "error"),
                        "anti_bot_or_blocked": getattr(exc, "kind", "") == "blocked",
                        "retrieved_at": now_iso(),
                        "snippets_are_not_evidence": True,
                    }
                )
                self._put_failure(candidate.url, message)
                continue
            if fetched.sha256 in seen_hashes:
                continue
            seen_hashes.add(fetched.sha256)
            candidate.retrieval_status = "retrieved"
            candidate.evidence.update(self._evidence(fetched, cache_hit=False, error=""))
            self._put(fetched)
            results.append(RetrievedDocument(candidate=candidate, fetched=fetched))
            if fetched.is_html and follow_links and len(results) < max_documents:
                # ``FetchedDocument.links`` is the authoritative link list from
                # the HTML parser. Never infer a PDF from page text.
                ordered = [
                    (url, label)
                    for url, label in fetched.links
                    if is_document_url(url, label)
                ][:follow_links]
                for document_url, label in ordered:
                    if len(results) >= max_documents:
                        break
                    try:
                        child = self._fetcher_for(document_url).fetch_document(document_url)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{document_url}: {type(exc).__name__}: {exc}"[:500])
                        continue
                    if child.sha256 in seen_hashes:
                        continue
                    seen_hashes.add(child.sha256)
                    candidate.evidence.setdefault("followed_documents", []).append(
                        {
                            "url": child.final_url,
                            "title": label or child.title,
                            "sha256": child.sha256,
                            "extraction_method": child.extraction_method,
                        }
                    )
                    self._put(child)
                    results.append(
                        RetrievedDocument(
                            candidate=candidate,
                            fetched=child,
                            followed_from=candidate.url,
                        )
                    )
        if errors and not results:
            candidate_error = "; ".join(errors[:3])
            for candidate in queue:
                if candidate.retrieval_status == "discovered":
                    candidate.retrieval_status = "failed"
                    candidate.evidence["retrieval_error"] = candidate_error
        return results

    @staticmethod
    def _evidence(
        fetched: FetchedDocument, *, cache_hit: bool, error: str
    ) -> dict[str, Any]:
        return {
            "content_sha256": fetched.sha256,
            "content_type": fetched.content_type,
            "final_url": fetched.final_url,
            "text_chars": len(fetched.text),
            "page_count": fetched.page_count,
            "extraction_method": fetched.extraction_method,
            "retrieval_http_status": fetched.http_status,
            "retrieved_at": fetched.retrieved_at,
            "cache_hit": cache_hit,
            "retrieval_error": error,
            "snippets_are_not_evidence": True,
            "warnings": fetched.warnings,
            "crawler": fetched.crawler,
            "browser_ms": fetched.browser_ms,
            "markdown_chars": len(fetched.markdown),
            "table_count": len(fetched.tables),
            "canonical_url": fetched.canonical_url,
            "metadata": fetched.metadata,
        }

    def _put(self, fetched: FetchedDocument) -> None:
        if self.cache:
            self.cache.put(fetched.cache_payload())

    def _put_failure(self, url: str, error: str) -> None:
        if self.cache:
            existing = self.cache.get(url)
            if existing and existing.get("status") == "ok":
                # Preserve a previously successful document when a later
                # selector change cannot re-fetch it; the current run records
                # the failure separately.
                return
            self.cache.put(
                {
                    "url": url,
                    "final_url": url,
                    "title": "",
                    "content_type": "",
                    "text": "",
                    "text_chars": 0,
                    "sha256": "",
                    "bytes_read": 0,
                    "page_count": None,
                    "extraction_method": "failed",
                    "http_status": None,
                    "warnings": [],
                    "fetched_at": now_iso(),
                    "markdown": "",
                    "links": [],
                    "tables": [],
                    "metadata": {},
                    "canonical_url": "",
                    "crawler": "http",
                    "browser_ms": None,
                    "status": "error",
                    "error": error[:1000],
                }
            )


__all__ = [
    "DatabaseRetrievalCache",
    "DocumentFetcher",
    "FetchedDocument",
    "RetrievedDocument",
    "RetrievalCache",
    "SourceRetriever",
    "ZipBundleFetcher",
    "extract_document_links",
    "html_to_text",
    "is_document_url",
]
