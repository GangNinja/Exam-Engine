"""Crawl4AI browser adapter for public exam-source pages.

The adapter is intentionally a retrieval component, not a search engine.  A
search provider finds candidate URLs; this module renders and extracts the
candidate pages.  PDFs are handled by the existing PyMuPDF pipeline because
Crawl4AI's browser path correctly refuses PDFs that look like anti-bot or empty
documents.

The browser is started lazily and reused across pages in a run.  A health check
validates both the Python package and the Chromium runtime without requiring a
remote site.  No stealth, CAPTCHA or authentication bypass is enabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import threading
import time
import urllib.parse
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any

from exam_engine.models.recovery import now_iso

logger = logging.getLogger(__name__)

_BLOCKED_MARKERS = (
    "blocked by anti-bot",
    "anti-bot",
    "captcha",
    "access denied",
    "forbidden",
    "cloudflare",
)


class Crawl4AIError(RuntimeError):
    """A page was not retrieved or extracted."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = "crawl_error"


class Crawl4AIBlocked(Crawl4AIError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.kind = "blocked"


class Crawl4AIEmpty(Crawl4AIError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.kind = "empty_content"


@dataclass
class CrawledPage:
    url: str
    final_url: str
    status_code: int
    success: bool
    title: str
    markdown: str
    text: str
    html: str
    links: list[dict[str, str]] = field(default_factory=list)
    tables: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    canonical_url: str = ""
    error: str = ""
    elapsed_ms: int = 0
    crawler: str = "crawl4ai"


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return str(value)[:2000]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth + 1) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(), depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value), depth + 1)
    return str(value)[:2000]


def _link_rows(raw: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw, dict):
        children: list[Any] = []
        for value in raw.values():
            if isinstance(value, list):
                children.extend(value)
        raw = children
    for item in raw or []:
        if isinstance(item, str):
            href, label = item, ""
        elif isinstance(item, dict):
            href = str(item.get("href") or item.get("url") or "").strip()
            label = str(item.get("text") or item.get("title") or "").strip()
        else:
            href = str(getattr(item, "href", "") or "").strip()
            label = str(getattr(item, "text", "") or "").strip()
        if not href or not href.startswith(("http://", "https://")) or href in seen:
            continue
        seen.add(href)
        rows.append({"url": href, "label": " ".join(label.split())[:500]})
    return rows[:500]


def result_to_page(result: Any, *, url: str, elapsed_ms: int = 0) -> CrawledPage:
    """Convert a Crawl4AI result object to a stable local structure."""

    success = bool(getattr(result, "success", False))
    status = int(getattr(result, "status_code", 0) or 0)
    error = str(getattr(result, "error_message", "") or "").strip()
    metadata_raw = getattr(result, "metadata", {}) or {}
    metadata = _jsonable(metadata_raw) if isinstance(metadata_raw, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {"raw": metadata}
    markdown = str(getattr(result, "markdown", "") or "")
    html = str(getattr(result, "html", "") or "")
    title = str(
        metadata.get("title")
        or metadata.get("og:title")
        or metadata.get("twitter:title")
        or ""
    ).strip()
    if not title:
        title = str(getattr(result, "title", "") or "").strip()
    links = _link_rows(getattr(result, "links", {}))
    tables = _jsonable(getattr(result, "tables", []) or [])
    if not isinstance(tables, list):
        tables = []
    final_url = str(getattr(result, "redirected_url", "") or getattr(result, "url", "") or url)
    canonical = str(
        metadata.get("canonical")
        or metadata.get("canonical_url")
        or metadata.get("og:url")
        or ""
    ).strip()
    page = CrawledPage(
        url=url,
        final_url=final_url,
        status_code=status,
        success=success,
        title=title,
        markdown=markdown,
        text=markdown,
        html=html,
        links=links,
        tables=tables,
        metadata=metadata,
        canonical_url=canonical,
        error=error,
        elapsed_ms=elapsed_ms,
    )
    return page


def _is_pdf_url(url: str) -> bool:
    return urllib.parse.urlparse(url).path.casefold().endswith(".pdf")


class Crawl4AISession:
    """A single reusable asynchronous Crawl4AI browser running in a thread."""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: float = 25.0,
        page_timeout: int = 22_000,
        user_agent: str | None = None,
    ) -> None:
        self.headless = bool(headless)
        self.timeout = max(5.0, min(float(timeout), 90.0))
        self.page_timeout = max(5_000, min(int(page_timeout), 90_000))
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._open_lock = threading.Lock()
        self._crawler: Any = None
        self._run_config: Any = None
        self._opened = False
        self._closed = False
        self._startup_error: Exception | None = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _ensure_thread(self) -> None:
        if self._closed:
            raise Crawl4AIError("Crawl4AI session has been closed")
        if self._thread is not None and self._thread.is_alive():
            return
        with self._open_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._thread_main, name="exam-engine-crawl4ai", daemon=True
            )
            self._thread.start()
        if not self._ready.wait(10):
            raise Crawl4AIError("Crawl4AI event loop did not start")
        if self._loop is None:
            raise Crawl4AIError("Crawl4AI event loop is unavailable")

    async def _open(self) -> None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        browser_config = BrowserConfig(
            headless=self.headless,
            user_agent=self.user_agent,
            verbose=False,
        )
        self._crawler = AsyncWebCrawler(config=browser_config)
        await self._crawler.__aenter__()
        run_kwargs: dict[str, Any] = {
            "page_timeout": self.page_timeout,
            "wait_until": "domcontentloaded",
            "max_retries": 1,
        }
        try:
            from crawl4ai import CacheMode

            run_kwargs["cache_mode"] = CacheMode.BYPASS
        except Exception:  # noqa: BLE001 - optional enum on older releases
            pass
        try:
            self._run_config = CrawlerRunConfig(**run_kwargs)
        except TypeError:
            # Keep compatibility with older Crawl4AI releases that do not accept
            # every keyword.  The retrieval timeout still bounds the operation.
            self._run_config = CrawlerRunConfig(page_timeout=self.page_timeout)
        self._opened = True

    def _ensure_open(self) -> None:
        self._ensure_thread()
        if self._opened:
            return
        with self._open_lock:
            if self._opened:
                return
            if self._startup_error is not None:
                raise Crawl4AIError(str(self._startup_error)) from self._startup_error
            assert self._loop is not None
            future = asyncio.run_coroutine_threadsafe(self._open(), self._loop)
            try:
                future.result(timeout=self.timeout + 15)
            except Exception as exc:  # noqa: BLE001
                self._startup_error = exc
                raise Crawl4AIError(
                    f"Crawl4AI browser initialization failed: {type(exc).__name__}: {exc}"
                ) from exc

    async def _crawl(self, url: str) -> Any:
        assert self._crawler is not None
        return await self._crawler.arun(url, config=self._run_config)

    def crawl(self, url: str) -> CrawledPage:
        self._ensure_open()
        assert self._loop is not None
        started = time.perf_counter()
        future = asyncio.run_coroutine_threadsafe(self._crawl(url), self._loop)
        try:
            result = future.result(timeout=self.timeout + 10)
        except FutureTimeoutError as exc:
            future.cancel()
            raise Crawl4AIError("Crawl4AI page timeout", status_code=None) from exc
        except Exception as exc:  # noqa: BLE001
            raise Crawl4AIError(
                f"Crawl4AI page failure: {type(exc).__name__}: {exc}"
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        page = result_to_page(result, url=url, elapsed_ms=elapsed_ms)
        if not page.success:
            lowered = page.error.casefold()
            if page.status_code in {401, 403, 429, 503} or any(
                marker in lowered for marker in _BLOCKED_MARKERS
            ):
                raise Crawl4AIBlocked(
                    page.error or f"HTTP {page.status_code} blocked the page",
                    status_code=page.status_code or None,
                )
            raise Crawl4AIError(
                page.error or f"Crawl4AI returned an unsuccessful result ({page.status_code})",
                status_code=page.status_code or None,
            )
        if not page.text.strip():
            raise Crawl4AIEmpty(
                "Crawl4AI returned no extractable page text",
                status_code=page.status_code or None,
            )
        return page

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._opened and self._loop is not None and self._crawler is not None:
            async def _close() -> None:
                try:
                    await self._crawler.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    logger.info("Crawl4AI close failed", exc_info=True)

            try:
                future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
                future.result(timeout=15)
            except Exception:  # noqa: BLE001
                logger.info("Crawl4AI close timed out", exc_info=True)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._opened = False


class Crawl4AIHealth:
    """Cached, explicit package and browser-runtime health diagnostics."""

    _cache: dict[str, Any] = {}
    _cache_at: float = 0.0

    @classmethod
    def quick_status(cls) -> dict[str, Any]:
        installed = importlib.util.find_spec("crawl4ai") is not None
        playwright = importlib.util.find_spec("playwright") is not None
        status: dict[str, Any] = {
            "status": "unknown",
            "installed": installed,
            "browser_package": playwright,
            "version": None,
            "browser_launch_ok": None,
            "headless": True,
            "checked_at": None,
            "error": "",
        }
        if installed:
            try:
                status["version"] = importlib.metadata.version("crawl4ai")
            except importlib.metadata.PackageNotFoundError:
                status["version"] = "unknown"
        if cls._cache:
            status.update(cls._cache)
            status["cached"] = True
        return status

    @classmethod
    def check(cls, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and cls._cache and now - cls._cache_at < 300:
            cached = dict(cls._cache)
            cached["cached"] = True
            return cached
        status = cls.quick_status()
        status.pop("cached", None)
        status["checked_at"] = now_iso()
        if not status["installed"]:
            status.update(
                status="unavailable",
                error="crawl4ai is not installed; install the crawl extra",
            )
            cls._cache, cls._cache_at = dict(status), now
            return status
        if not status["browser_package"]:
            status.update(
                status="browser_missing",
                error="playwright is not installed; run python -m playwright install chromium",
            )
            cls._cache, cls._cache_at = dict(status), now
            return status

        async def _probe() -> tuple[bool, str, str, str]:
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                try:
                    executable = playwright.chromium.executable_path
                except Exception:  # noqa: BLE001
                    executable = ""
                browser = await playwright.chromium.launch(headless=True)
                try:
                    version = browser.version
                finally:
                    await browser.close()
                return True, version, executable, ""

        try:
            ok, browser_version, executable, error = asyncio.run(_probe())
        except Exception as exc:  # noqa: BLE001
            ok, browser_version, executable, error = (
                False,
                "",
                "",
                f"{type(exc).__name__}: {exc}"[:1000],
            )
        status.update(
            status="ok" if ok else "browser_launch_failed",
            browser_launch_ok=ok,
            browser_version=browser_version,
            executable_path=executable,
            error=error,
        )
        cls._cache, cls._cache_at = dict(status), now
        return status


class Crawl4AIFetcher:
    """Document fetcher using Crawl4AI for HTML and the PDF pipeline for PDFs."""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout: float = 25.0,
        page_timeout: int = 22_000,
        pdf_fetcher: Any = None,
        session: Crawl4AISession | None = None,
    ) -> None:
        self.session = session or Crawl4AISession(
            headless=headless, timeout=timeout, page_timeout=page_timeout
        )
        self._pdf_fetcher = pdf_fetcher
        self.last_error: str = ""

    @staticmethod
    def available() -> bool:
        return (
            importlib.util.find_spec("crawl4ai") is not None
            and importlib.util.find_spec("playwright") is not None
        )

    @staticmethod
    def health(*, force: bool = False) -> dict[str, Any]:
        return Crawl4AIHealth.check(force=force)

    def _pdf(self) -> Any:
        if self._pdf_fetcher is None:
            from exam_engine.recovery.retrieval import DocumentFetcher

            self._pdf_fetcher = DocumentFetcher()
        return self._pdf_fetcher

    @staticmethod
    def _validate(url: str) -> str:
        from exam_engine.recovery.retrieval import _safe_url

        return _safe_url(url)

    def crawl(self, url: str) -> CrawledPage:
        url = self._validate(url)
        return self.session.crawl(url)

    def fetch_document(self, url: str) -> Any:
        """Return a FetchedDocument, using PyMuPDF for PDF URLs."""

        from exam_engine.recovery.retrieval import FetchedDocument, _MAX_TEXT

        url = self._validate(url)
        if _is_pdf_url(url):
            document = self._pdf().fetch_document(url)
            if hasattr(document, "crawler"):
                document.crawler = "pdf_pipeline"
            return document
        try:
            page = self.crawl(url)
            # Validate a redirect target too; an open redirect must not turn a
            # public document request into a private-network fetch.
            page.final_url = self._validate(page.final_url)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            raise
        raw = page.html or page.markdown
        raw_bytes = raw.encode("utf-8", "ignore")
        extracted = page.text[: _MAX_TEXT]
        return FetchedDocument(
            url=url,
            final_url=page.final_url,
            title=page.title,
            content_type="text/html",
            text=extracted,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            bytes_read=len(raw_bytes),
            page_count=1,
            extraction_method="crawl4ai_markdown",
            links=[(item["url"], item.get("label", "")) for item in page.links],
            warnings=[] if page.tables else ["page produced no table extraction"],
            retrieved_at=now_iso(),
            http_status=page.status_code or 200,
            markdown=page.markdown,
            tables=page.tables,
            metadata=page.metadata,
            canonical_url=page.canonical_url,
            crawler="crawl4ai",
            browser_ms=page.elapsed_ms,
        )

    def close(self) -> None:
        self.session.close()


__all__ = [
    "CrawledPage",
    "Crawl4AIBlocked",
    "Crawl4AIEmpty",
    "Crawl4AIError",
    "Crawl4AIFetcher",
    "Crawl4AIHealth",
    "Crawl4AISession",
    "result_to_page",
]
