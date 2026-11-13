"""Web retrieval for the tutor.

Exa is used when an API key is configured. Without a key the tutor falls back to
Wikipedia's public search and summary APIs, which need no account. Both paths
return cited snippets; the tutor never presents a search result as a guaranteed
fact.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from exam_engine.config import TutorSettings

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "define",
        "definition",
        "do",
        "does",
        "explain",
        "for",
        "from",
        "give",
        "how",
        "in",
        "is",
        "it",
        "me",
        "of",
        "on",
        "or",
        "please",
        "tell",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)
_CACHE_SECONDS = 300.0
_CACHE_LOCK = threading.Lock()
_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
_DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"
_USER_AGENT = "ExamEngine/0.1 (local educational tutor; contact: local user)"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass(frozen=True)
class WebSource:
    source_id: str
    title: str
    url: str
    snippet: str
    provider: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "provider": self.provider,
        }

    def as_citation(self) -> dict[str, Any]:
        return {
            "kind": "web",
            "title": self.title,
            "url": self.url,
            "document_title": self.title,
            "excerpt": self.snippet[:1200],
            "method": f"{self.provider}_web_search",
        }


_CACHE: dict[tuple[str, int, str], tuple[float, list[WebSource]]] = {}
_SUMMARY_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_SUMMARY_LOCK = threading.Lock()


class _DuckDuckGoParser(HTMLParser):
    """Extract result titles, target URLs and snippets from DDG's HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if "result__a" in classes:
            self.results.append(
                {
                    "title": "",
                    "url": str(values.get("href") or ""),
                    "snippet": "",
                }
            )
            self._in_title = True
        elif "result__snippet" in classes and self.results:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_title = False
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if not self.results:
            return
        if self._in_title:
            self.results[-1]["title"] += data
        elif self._in_snippet:
            self.results[-1]["snippet"] += data


class WebSearch:
    """Search the public web with a no-key fallback and a short-lived cache."""

    def __init__(
        self,
        settings: TutorSettings,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.env = os.environ if env is None else env

    @property
    def available(self) -> bool:
        return (self.settings.web_search or "auto").strip().casefold() not in {
            "off",
            "none",
            "disabled",
        }

    def status(self) -> dict[str, Any]:
        mode = (self.settings.web_search or "auto").strip().casefold()
        if not self.available:
            return {
                "available": False,
                "provider": "off",
                "detail": "Web search is disabled by configuration.",
            }
        provider = (
            "exa"
            if mode in {"auto", "exa"} and self.env.get("EXA_API_KEY")
            else "wikipedia"
        )
        return {
            "available": True,
            "provider": provider,
            "detail": (
                "Uses Exa semantic search with citations."
                if provider == "exa"
                else "Uses Wikipedia article extracts with source links."
            ),
        }

    def search(self, question: str, *, limit: int | None = None) -> list[WebSource]:
        if not self.available:
            return []
        limit = max(1, min(limit or self.settings.max_web_sources, 6))
        queries = _search_queries(question)
        if not queries:
            return []
        cache_key = (tuple(queries), limit, (self.settings.web_search or "auto").casefold())
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None and now - cached[0] < _CACHE_SECONDS:
                return list(cached[1])

        mode = (self.settings.web_search or "auto").strip().casefold()
        sources: list[WebSource] = []
        if mode in {"auto", "exa"} and self.env.get("EXA_API_KEY"):
            sources = self._exa(queries[0], limit)
        if not sources and mode in {"auto", "wikipedia"}:
            # One search plus the focused article extracts keeps this well inside
            # the latency budget and avoids the public API's burst rate limit.
            sources = self._wikipedia(
                _search_query(question, max_tokens=6),
                limit,
                extras=_wikipedia_extras(question),
            )
        if not sources and mode in {"auto", "duckduckgo", "exa"}:
            # Keyless fallback for environments where Wikipedia is unreachable.
            per_query = max(2, limit // len(queries))
            with ThreadPoolExecutor(max_workers=min(3, len(queries))) as pool:
                batches = list(
                    pool.map(lambda item: self._duckduckgo(item, per_query), queries)
                )
            sources = _merge_web_sources(batches, limit)
        if sources:
            with _CACHE_LOCK:
                _CACHE[cache_key] = (now, list(sources))
        return sources

    def _exa(self, query: str, limit: int) -> list[WebSource]:
        api_key = (self.env.get("EXA_API_KEY") or "").strip()
        if not api_key:
            return []
        payload = {
            "query": query,
            "numResults": limit,
            "contents": {
                "text": {"maxCharacters": 1200},
                "highlights": {"numSentences": 2},
            },
        }
        request = urllib.request.Request(
            "https://api.exa.ai/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=6) as response:  # noqa: S310
                body = json.load(response)
        except (OSError, TimeoutError, ValueError) as exc:
            logger.info("Exa search failed: %s", type(exc).__name__)
            return []
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        sources: list[WebSource] = []
        for item in rows[:limit]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Web source").strip()
            url = str(item.get("url") or "").strip()
            highlights = item.get("highlights")
            if isinstance(highlights, list):
                snippet = " ".join(str(part) for part in highlights)
            else:
                snippet = str(item.get("text") or "")
            snippet = _clean(snippet)
            if url and snippet:
                sources.append(
                    WebSource(
                        source_id=f"W{len(sources) + 1}",
                        title=title,
                        url=url,
                        snippet=snippet[:1100],
                        provider="exa",
                    )
                )
        return sources

    def _duckduckgo(self, query: str, limit: int) -> list[WebSource]:
        """Read public DuckDuckGo result snippets without an API key.

        The HTML endpoint is a lightweight search page. It gives topic-focused
        snippets for ordinary study questions, which is often more useful than a
        single encyclopedia lead paragraph, and each result keeps its source URL.
        """

        url = f"{_DUCKDUCKGO_HTML}?{urllib.parse.urlencode({'q': query})}"
        request = urllib.request.Request(url, headers={"User-Agent": _BROWSER_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:  # noqa: S310
                body = response.read().decode("utf-8", "replace")
        except (OSError, TimeoutError, ValueError) as exc:
            logger.info("DuckDuckGo search failed: %s", type(exc).__name__)
            return []
        parser = _DuckDuckGoParser()
        try:
            parser.feed(body)
        except ValueError:
            return []
        sources: list[WebSource] = []
        for row in parser.results:
            if len(sources) >= limit:
                break
            title = _clean(row["title"])
            snippet = _clean(row["snippet"])
            target = _decode_result_url(row["url"])
            if not title or not snippet or not target:
                continue
            sources.append(
                WebSource(
                    source_id=f"W{len(sources) + 1}",
                    title=title,
                    url=target,
                    snippet=snippet,
                    provider="duckduckgo",
                )
            )
        return sources

    def _wikipedia(
        self,
        query: str,
        limit: int,
        *,
        extras: list[str] | None = None,
    ) -> list[WebSource]:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": "0",
            "srlimit": str(max(limit, 3)),
            "format": "json",
            "utf8": "1",
        }
        search_url = f"{_WIKIPEDIA_API}?{urllib.parse.urlencode(params)}"
        try:
            payload = _get_json(search_url)
        except (OSError, TimeoutError, ValueError) as exc:
            logger.info("Wikipedia search failed: %s", type(exc).__name__)
            return []
        rows = payload.get("query", {}).get("search") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        titles: list[str] = []
        for title in extras or []:
            if title and title not in titles:
                titles.append(title)
        for item in rows[: max(limit, 3)]:
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                if title and title not in titles:
                    titles.append(title)
        return self._extracts(titles[: min(3, max(1, limit))], limit)

    def _extracts(self, titles: list[str], limit: int) -> list[WebSource]:
        """Fetch full extracts one title at a time.

        The public API returns only one non-empty extract when several titles are
        requested together. Sequential fetches are still fast enough for a
        three-source answer and avoid an incomplete result set.
        """

        sources: list[WebSource] = []
        for index, title in enumerate(titles):
            if len(sources) >= limit:
                break
            # Two full articles give the answer enough technical detail; the
            # remaining sources use the lighter summary endpoint so a burst of
            # full-extract requests cannot exhaust the public API's rate limit.
            summary = self._summary_cached(title) if index < 2 else None
            if summary is None:
                summary = self._rest_summary_cached(title)
            if summary is None:
                continue
            sources.append(
                WebSource(
                    source_id=f"W{len(sources) + 1}",
                    title=title,
                    url=summary["url"],
                    snippet=summary["extract"][:12000],
                    provider="wikipedia",
                )
            )
        return sources

    @staticmethod
    def _summary_cached(title: str) -> dict[str, str] | None:
        key = f"full:{title.casefold()}"
        now = time.monotonic()
        with _SUMMARY_LOCK:
            cached = _SUMMARY_CACHE.get(key)
            if cached is not None and now - cached[0] < _CACHE_SECONDS:
                return cached[1]
        summary = WebSearch._summary(title)
        if summary is not None:
            with _SUMMARY_LOCK:
                _SUMMARY_CACHE[key] = (now, summary)
        return summary

    @staticmethod
    def _rest_summary_cached(title: str) -> dict[str, str] | None:
        key = f"rest:{title.casefold()}"
        now = time.monotonic()
        with _SUMMARY_LOCK:
            cached = _SUMMARY_CACHE.get(key)
            if cached is not None and now - cached[0] < _CACHE_SECONDS:
                return cached[1]
        encoded = urllib.parse.quote(title.replace(" ", "_"), safe="")
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
        try:
            payload = _get_json(url)
        except (OSError, TimeoutError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        extract = _clean(str(payload.get("extract") or ""))
        if not extract:
            return None
        urls = payload.get("content_urls") or {}
        desktop = urls.get("desktop") if isinstance(urls, dict) else None
        page_url = str(desktop.get("page") or "") if isinstance(desktop, dict) else ""
        summary = {
            "extract": extract,
            "url": page_url or f"https://en.wikipedia.org/wiki/{encoded}",
        }
        with _SUMMARY_LOCK:
            _SUMMARY_CACHE[key] = (now, summary)
        return summary

    @staticmethod
    def _summary(title: str) -> dict[str, str] | None:
        """Fetch one article extract; retained for focused callers and tests."""

        params = {
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "exsectionformat": "plain",
            "redirects": "1",
            "titles": title,
            "format": "json",
            "utf8": "1",
        }
        try:
            payload = _get_json(f"{_WIKIPEDIA_API}?{urllib.parse.urlencode(params)}")
        except (OSError, TimeoutError, ValueError):
            return None
        pages = payload.get("query", {}).get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, dict):
            return None
        extract = ""
        for page in pages.values():
            if isinstance(page, dict):
                extract = _clean(str(page.get("extract") or ""))
                if extract:
                    break
        if not extract:
            return None
        encoded = urllib.parse.quote(title.replace(" ", "_"), safe="")
        return {"extract": extract, "url": f"https://en.wikipedia.org/wiki/{encoded}"}


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=6) as response:  # noqa: S310
        return json.load(response)


def _decode_result_url(value: str) -> str:
    """Resolve DuckDuckGo's ``/l/?uddg=`` redirect wrapper to the result URL."""

    url = html.unescape(value).strip()
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/"):
        url = f"https://duckduckgo.com{url}"
    parsed = urllib.parse.urlsplit(url)
    if parsed.path.startswith("/l/") or "uddg=" in parsed.query:
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            url = urllib.parse.unquote(target[0])
    return url if url.startswith(("http://", "https://")) else ""


def _search_query(question: str, *, max_tokens: int = 6) -> str:
    tokens = [
        token
        for token in _WORD_RE.findall(question.casefold())
        if token not in _STOPWORDS and len(token) > 1
    ]
    return " ".join(tokens[:max_tokens])


def _wikipedia_extras(question: str) -> list[str]:
    lowered = question.casefold()
    extras: list[str] = []
    if any(
        phrase in lowered
        for phrase in ("gradually varied", "water surface", "surface profile", "uniform flow")
    ):
        extras.append("Standard step method")
    return extras


def _merge_web_sources(batches: list[list[WebSource]], limit: int) -> list[WebSource]:
    by_url: dict[str, WebSource] = {}
    for batch in batches:
        for source in batch:
            existing = by_url.get(source.url)
            if existing is None or len(source.snippet) > len(existing.snippet):
                by_url[source.url] = source
    sources: list[WebSource] = []
    for index, source in enumerate(by_url.values(), start=1):
        sources.append(
            WebSource(
                source_id=f"W{index}",
                title=source.title,
                url=source.url,
                snippet=source.snippet,
                provider=source.provider,
            )
        )
        if len(sources) >= limit:
            break
    return sources


def _search_queries(question: str) -> list[str]:
    """Build up to three complementary study queries from one long question."""

    tokens = [
        token
        for token in _WORD_RE.findall(question.casefold())
        if token not in _STOPWORDS and len(token) > 1
    ]
    if not tokens:
        return []
    lowered = question.casefold()
    queries: list[str] = [" ".join(tokens[:8])]
    if "specific energy" in lowered:
        queries.append("specific energy formula E y V2 2g open channel")
    if "critical flow" in lowered:
        queries.append("critical flow Froude number open channel")
    if "hydraulic jump" in lowered:
        queries.append("hydraulic jump supercritical subcritical energy dissipation")
    if "uniform flow" in lowered:
        queries.append("uniform flow open channel")
    if any(
        phrase in lowered
        for phrase in ("gradually varied", "water surface", "surface profile")
    ):
        queries.append("gradually varied flow water surface profiles")
    if len(queries) == 1 and len(tokens) > 8:
        queries.append(" ".join(tokens[8:16]))
    result: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            result.append(query)
    return result[:6]


def _clean(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value))
    # MediaWiki occasionally returns replacement characters around quotes in
    # plain-text extracts; keep the sentence readable instead of showing �.
    text = text.replace("\ufffd", "'")
    return " ".join(text.split())


__all__ = ["WebSearch", "WebSource"]
