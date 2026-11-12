"""Search-provider adapters for Phase 14.2 source discovery.

The adapters are deliberately small and replaceable.  A provider returns only
discovery leads: title, URL and snippet.  Retrieval and answer evidence are
handled separately by :mod:`exam_engine.recovery.retrieval`; a search snippet is
never accepted as an answer.

Provider order for ``auto`` is Exa (when a key is configured), DuckDuckGo HTML
(keyless), then Wikipedia (keyless, retained as a last resort).  Every network
call is bounded, rate-limited and retried conservatively.  No provider bypasses
authentication, CAPTCHAs, paywalls or robots restrictions.
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
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any, Mapping, Protocol

from exam_engine.config import TutorSettings

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DDG_RESULT_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_USER_AGENT = "ExamEngine/0.2 (local educational source discovery; contact: local user)"


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value or ""))).strip()


def _attrs(raw: str) -> dict[str, str]:
    return {
        match.group(1).casefold(): html.unescape(match.group(2) or match.group(3) or "")
        for match in _ATTR_RE.finditer(raw or "")
    }


@dataclass(frozen=True)
class SearchHit:
    """One public search result; never answer evidence by itself."""

    title: str
    url: str
    snippet: str = ""
    provider: str = "unknown"

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "provider": self.provider,
        }


class SearchProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]: ...


def decode_duckduckgo_url(url: str) -> str:
    """Unwrap DuckDuckGo's redirect URL without following it."""

    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.netloc.casefold().endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return value


@dataclass
class _CacheEntry:
    expires_at: float
    hits: list[SearchHit]


class DuckDuckGoSearch:
    """Keyless HTML search adapter with local caching and rate limiting.

    DuckDuckGo's HTML endpoint is a public search page.  The adapter sends a
    descriptive user agent, waits between requests, and stops on an error or an
    anti-bot page rather than attempting to bypass it.
    """

    name = "duckduckgo"

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        delay_seconds: float = 1.2,
        retries: int = 2,
        cache_seconds: float = 900.0,
        endpoint: str = _DDG_ENDPOINT,
    ) -> None:
        self.timeout = max(3.0, min(float(timeout), 45.0))
        self.delay_seconds = max(0.0, min(float(delay_seconds), 15.0))
        self.retries = max(0, min(int(retries), 4))
        self.cache_seconds = max(30.0, float(cache_seconds))
        self.endpoint = endpoint
        self._cache: dict[tuple[str, int], _CacheEntry] = {}
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.last_error: str | None = None
        self.last_status: int | None = None

    @property
    def available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": True,
            "kind": "keyless_public_search",
            "detail": "DuckDuckGo HTML search; bounded and cached; snippets are leads only.",
            "last_error": self.last_error,
            "last_http_status": self.last_status,
        }

    def _throttle(self) -> None:
        with self._lock:
            wait = self.delay_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _request(self, query: str) -> str:
        params = urllib.parse.urlencode({"q": query, "kl": "wt-wt"})
        request = urllib.request.Request(
            f"{self.endpoint}?{params}",
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.8",
            },
        )
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    self.last_status = int(getattr(response, "status", 200) or 200)
                    payload = response.read(3_000_000)
                    if len(payload) >= 3_000_000:
                        raise ValueError("search response exceeded the configured size")
                    return payload.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                self.last_status = int(exc.code)
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (OSError, TimeoutError, ValueError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(min(8.0, 1.0 * (2**attempt)))
        self.last_error = type(last_error).__name__ if last_error else None
        logger.info("DuckDuckGo search failed: %s", type(last_error).__name__ if last_error else "empty")
        return ""

    @staticmethod
    def parse_results(payload: str, *, limit: int) -> list[SearchHit]:
        anchors: list[tuple[str, str]] = []
        snippets: list[str] = []
        for match in _DDG_RESULT_RE.finditer(payload or ""):
            attrs = _attrs(match.group(1))
            classes = attrs.get("class", "").split()
            label = _clean(match.group(2))
            if "result__a" in classes and attrs.get("href"):
                anchors.append((attrs["href"], label))
            elif "result__snippet" in classes and label:
                snippets.append(label)
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for index, (href, label) in enumerate(anchors):
            url = decode_duckduckgo_url(href)
            if not url or not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            snippet = snippets[index] if index < len(snippets) else ""
            hits.append(
                SearchHit(
                    title=label or url,
                    url=url,
                    snippet=snippet[:1200],
                    provider="duckduckgo",
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        query = " ".join(str(query or "").split())
        if not query:
            return []
        limit = max(1, min(int(limit or 5), 10))
        key = (query.casefold(), limit)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                return list(cached.hits)
        payload = self._request(query)
        hits = self.parse_results(payload, limit=limit)
        if self.last_status == 202 and not hits:
            self.last_error = "HTTP 202 anti-bot/challenge (no results accepted)"
        elif self.last_status in {403, 429} and not hits:
            self.last_error = f"HTTP {self.last_status} blocked or rate-limited"
        elif hits:
            self.last_error = None
        with self._lock:
            self._cache[key] = _CacheEntry(expires_at=now + self.cache_seconds, hits=hits)
        return hits


class ExaSearch:
    """Exa semantic search; available only when ``EXA_API_KEY`` is configured."""

    name = "exa"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 15.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if env is None else env
        self.api_key = (api_key or env.get("EXA_API_KEY") or "").strip()
        self.timeout = max(3.0, min(float(timeout), 45.0))
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "kind": "keyed_semantic_search",
            "detail": (
                "Exa semantic search with cited snippets."
                if self.available
                else "EXA_API_KEY is not configured."
            ),
            "last_error": self.last_error,
        }

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        if not self.available:
            return []
        limit = max(1, min(int(limit or 5), 10))
        payload = {
            "query": query,
            "numResults": limit,
            "contents": {"text": {"maxCharacters": 1200}},
        }
        request = urllib.request.Request(
            "https://api.exa.ai/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = json.loads(response.read(2_000_000).decode("utf-8", "replace"))
        except (OSError, TimeoutError, ValueError) as exc:
            self.last_error = type(exc).__name__
            logger.info("Exa search failed: %s", self.last_error)
            return []
        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for item in rows[:limit]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = _clean(str(item.get("title") or url))
            snippet = _clean(str(item.get("text") or ""))
            if url and url.startswith(("http://", "https://")):
                hits.append(
                    SearchHit(
                        title=title or url,
                        url=url,
                        snippet=snippet[:1200],
                        provider=self.name,
                    )
                )
        return hits


class WikipediaSearch:
    """Last-resort keyless reference search, reusing the existing tutor client."""

    name = "wikipedia"

    def __init__(self, settings: Any = None, *, env: Mapping[str, str] | None = None) -> None:
        tutor = getattr(settings, "tutor", settings) if settings is not None else TutorSettings()
        self._settings = dataclass_replace(tutor, web_search="wikipedia")
        # Lazy import: the dashboard package imports the recovery service, so a
        # top-level dashboard import would create a recovery -> dashboard cycle.
        from exam_engine.dashboard.websearch import WebSearch

        self._client = WebSearch(self._settings, env=env)
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return self._client.available

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "kind": "keyless_reference_search",
            "detail": "Wikipedia search and summaries; not an examination-document archive.",
            "last_error": self.last_error,
        }

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        try:
            rows = self._client.search(query, limit=limit or 3)
        except Exception as exc:  # noqa: BLE001 - optional external retrieval
            self.last_error = type(exc).__name__
            logger.info("Wikipedia search failed: %s", self.last_error)
            return []
        return [
            SearchHit(
                title=row.title,
                url=row.url,
                snippet=row.snippet,
                provider="wikipedia",
            )
            for row in rows
            if row.url
        ]


class ProviderChain:
    """Try providers in order, merge results and remove duplicate URLs."""

    def __init__(self, providers: list[SearchProvider], *, name: str = "auto") -> None:
        self.providers = providers
        self.name = name

    @property
    def available(self) -> bool:
        return any(provider.available for provider in self.providers)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "kind": "provider_chain",
            "providers": [provider.status() for provider in self.providers],
            "detail": "Ordered provider chain; first results win and duplicates are removed.",
        }

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        limit = max(1, min(int(limit or 5), 10))
        merged: list[SearchHit] = []
        seen: set[str] = set()
        for provider in self.providers:
            if not provider.available or len(merged) >= limit:
                continue
            try:
                rows = provider.search(query, limit=limit)
            except Exception:  # noqa: BLE001 - one provider must not stop the run
                logger.warning("search provider %s failed", provider.name, exc_info=True)
                continue
            for row in rows:
                normalized = row.url.rstrip("/").casefold()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                merged.append(row)
                if len(merged) >= limit:
                    break
        return merged


def build_search_provider(
    name: str | None = None,
    *,
    settings: Any = None,
    env: Mapping[str, str] | None = None,
) -> SearchProvider | None:
    """Build a provider from a CLI/API name or ``EXAM_ENGINE_RECOVERY_PROVIDER``."""

    env = os.environ if env is None else env
    mode = (name or env.get("EXAM_ENGINE_RECOVERY_PROVIDER") or "auto").strip().casefold()
    if mode in {"off", "none", "disabled", ""}:
        return None
    timeout = float(env.get("EXAM_ENGINE_RECOVERY_SEARCH_TIMEOUT", "15") or 15)
    delay = float(env.get("EXAM_ENGINE_RECOVERY_SEARCH_DELAY", "1.2") or 1.2)
    if mode in {"ddg", "duckduckgo", "html"}:
        return DuckDuckGoSearch(timeout=timeout, delay_seconds=delay)
    if mode == "exa":
        return ExaSearch(timeout=timeout, env=env)
    if mode in {"wikipedia", "wiki"}:
        return WikipediaSearch(settings, env=env)
    if mode != "auto":
        raise ValueError(
            "search provider must be auto, exa, duckduckgo, wikipedia or off"
        )
    providers: list[SearchProvider] = []
    exa = ExaSearch(timeout=timeout, env=env)
    if exa.available:
        providers.append(exa)
    providers.append(DuckDuckGoSearch(timeout=timeout, delay_seconds=delay))
    providers.append(WikipediaSearch(settings, env=env))
    return ProviderChain(providers)


def provider_descriptions(settings: Any = None, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    env = os.environ if env is None else env
    return [
        ExaSearch(env=env).status(),
        DuckDuckGoSearch().status(),
        WikipediaSearch(settings, env=env).status(),
    ]


__all__ = [
    "DuckDuckGoSearch",
    "ExaSearch",
    "ProviderChain",
    "SearchHit",
    "SearchProvider",
    "WikipediaSearch",
    "build_search_provider",
    "decode_duckduckgo_url",
    "provider_descriptions",
]
