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
from dataclasses import dataclass
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
_WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_USER_AGENT = "ExamEngine/0.1 (local educational tutor; contact: local user)"


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
            "excerpt": self.snippet,
            "method": f"{self.provider}_web_search",
        }


_CACHE: dict[tuple[str, int, str], tuple[float, list[WebSource]]] = {}


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
        provider = "exa" if mode in {"auto", "exa"} and self.env.get("EXA_API_KEY") else "wikipedia"
        return {
            "available": True,
            "provider": provider,
            "detail": (
                "Uses Exa semantic search with citations."
                if provider == "exa"
                else "Uses Wikipedia search and article summaries, with source links."
            ),
        }

    def search(self, question: str, *, limit: int | None = None) -> list[WebSource]:
        if not self.available:
            return []
        limit = max(1, min(limit or self.settings.max_web_sources, 6))
        query = _search_query(question)
        if not query:
            return []
        cache_key = (query.casefold(), limit, (self.settings.web_search or "auto").casefold())
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None and now - cached[0] < _CACHE_SECONDS:
                return list(cached[1])

        mode = (self.settings.web_search or "auto").strip().casefold()
        sources: list[WebSource] = []
        if mode in {"auto", "exa"} and self.env.get("EXA_API_KEY"):
            sources = self._exa(query, limit)
        if not sources and mode in {"auto", "wikipedia", "exa"}:
            sources = self._wikipedia(query, limit)
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
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
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

    def _wikipedia(self, query: str, limit: int) -> list[WebSource]:
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
        sources: list[WebSource] = []
        for item in rows[: max(limit * 2, 4)]:
            if len(sources) >= limit:
                break
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            summary = self._summary(title)
            if summary is None:
                continue
            sources.append(
                WebSource(
                    source_id=f"W{len(sources) + 1}",
                    title=title,
                    url=summary["url"],
                    snippet=summary["extract"][:1100],
                    provider="wikipedia",
                )
            )
        return sources

    @staticmethod
    def _summary(title: str) -> dict[str, str] | None:
        encoded = urllib.parse.quote(title.replace(" ", "_"), safe="")
        try:
            payload = _get_json(f"{_WIKIPEDIA_SUMMARY}{encoded}")
        except (OSError, TimeoutError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        extract = _clean(str(payload.get("extract") or ""))
        if not extract:
            return None
        urls = payload.get("content_urls") or {}
        desktop = urls.get("desktop") if isinstance(urls, dict) else None
        url = str(desktop.get("page") or "") if isinstance(desktop, dict) else ""
        if not url:
            url = f"https://en.wikipedia.org/wiki/{encoded}"
        return {"extract": extract, "url": url}


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.load(response)


def _search_query(question: str) -> str:
    tokens = [
        token
        for token in _WORD_RE.findall(question.casefold())
        if token not in _STOPWORDS and len(token) > 1
    ]
    return " ".join(tokens[:12])


def _clean(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value))
    return " ".join(text.split())


__all__ = ["WebSearch", "WebSource"]
