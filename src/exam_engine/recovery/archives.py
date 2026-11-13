"""Configurable official examination archive discovery.

Search engines are unreliable for document discovery in this environment.  This
module queries explicitly configured, public official archives and returns only
document URLs found in those archives.  It never treats a page title or snippet
as answer evidence.

Three providers are supported:

``wordpress_media``
    A public WordPress media API (the GATE 2024 archive).
``html_archive``
    A public archive page whose document links are parsed for a configured
    year, class and subject (CBSE question papers/marking schemes and the JEE
    Advanced past-paper archive).
``document_urls``
    Explicitly configured official document URLs.  Used only where the authority
    publishes a stable document but no browsable archive (documented NTA keys).

Examination-specific metadata lives in ``archive_sources.json``.  The core code
has no GATE/CBSE/JEE/NEET paths or subject codes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from exam_engine.models.recovery import SourceCandidate, now_iso
from exam_engine.recovery.retrieval import DocumentFetcher

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
_DOCUMENT_SUFFIXES = (".pdf", ".zip")


def _normal(value: str) -> str:
    return _WS_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", (value or "").casefold())).strip()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:20]


@dataclass(frozen=True)
class ArchiveSpec:
    archive_id: str
    provider: str
    exam_terms: tuple[str, ...]
    year: int
    years: tuple[int, ...]
    base_url: str
    page_url: str
    page_urls: tuple[str, ...]
    media_api: str
    subject_codes: dict[str, tuple[str, ...]]
    subjects: dict[str, tuple[tuple[str, str], ...]]
    paper_title_pattern: str
    final_key_title: str
    provisional_key_title: str
    max_media: int
    class_code: str
    subject_mode: str
    language: str
    session_aware: bool
    set_aware: bool
    documents: tuple[dict[str, Any], ...]
    available: bool
    matchable: bool
    unavailable_reason: str
    key_availability: str
    year_order: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ArchiveSpec":
        year = int(raw.get("year") or 0)
        raw_years = raw.get("years") or ([year] if year else [])
        years = tuple(int(item) for item in raw_years if str(item).strip().isdigit())
        if not years and year:
            years = (year,)
        page_url = str(raw.get("page_url") or raw.get("base_url") or "")
        raw_page_urls = raw.get("page_urls") or ([page_url] if page_url else [])
        subjects: dict[str, tuple[tuple[str, str], ...]] = {}
        for key, value in (raw.get("subjects") or {}).items():
            rows: list[tuple[str, str]] = []
            if isinstance(value, dict):
                variant = str(value.get("variant") or "")
                for alias in value.get("aliases") or []:
                    rows.append((str(alias), variant))
            elif isinstance(value, list):
                for alias in value:
                    if isinstance(alias, dict):
                        rows.append(
                            (str(alias.get("alias") or ""), str(alias.get("variant") or ""))
                        )
                    else:
                        rows.append((str(alias), ""))
            subjects[_normal(str(key))] = tuple(rows)
        return cls(
            archive_id=str(raw["id"]),
            provider=str(raw.get("provider") or "wordpress_media"),
            exam_terms=tuple(str(item).casefold() for item in raw.get("exam_terms", [])),
            year=year,
            years=years,
            base_url=str(raw.get("base_url") or "").rstrip("/"),
            page_url=page_url,
            page_urls=tuple(str(item) for item in raw_page_urls if str(item).strip()),
            media_api=str(raw.get("media_api") or ""),
            subject_codes={
                _normal(key): tuple(str(code) for code in codes)
                for key, codes in (raw.get("subject_codes") or {}).items()
            },
            subjects=subjects,
            paper_title_pattern=str(raw.get("paper_title_pattern") or "{code}"),
            final_key_title=str(raw.get("final_key_title") or "{code}FinalAnswerKey"),
            provisional_key_title=str(raw.get("provisional_key_title") or "{code}key"),
            max_media=max(1, min(int(raw.get("max_media") or 100), 100)),
            class_code=str(raw.get("class_code") or ""),
            subject_mode=str(raw.get("subject_mode") or "subject"),
            language=str(raw.get("language") or "English"),
            session_aware=bool(raw.get("session_aware", False)),
            set_aware=bool(raw.get("set_aware", False)),
            documents=tuple(
                item for item in (raw.get("documents") or []) if isinstance(item, dict)
            ),
            available=bool(raw.get("available", True)),
            matchable=bool(raw.get("matchable", True)),
            unavailable_reason=str(raw.get("unavailable_reason") or ""),
            key_availability=str(raw.get("key_availability") or ""),
            year_order=str(raw.get("year_order") or "newest").casefold(),
        )

    @property
    def year_set(self) -> set[int]:
        return set(self.years) or ({self.year} if self.year else set())

    def status(self) -> dict[str, Any]:
        if not self.available:
            state = "unavailable"
        elif not self.matchable:
            state = "documented_unmatchable"
        else:
            state = "configured"
        return {
            "id": self.archive_id,
            "provider": self.provider,
            "year": self.year,
            "years": sorted(self.year_set),
            "base_url": self.base_url,
            "page_url": self.page_url,
            "subject_count": len(self.subject_codes) or len(self.subjects),
            "available": self.available,
            "matchable": self.matchable,
            "coverage_state": state,
            "reason": self.unavailable_reason,
            "key_availability": self.key_availability,
            "year_order": self.year_order,
        }


def load_archive_specs(path: Path | str | None = None) -> list[ArchiveSpec]:
    source = Path(path) if path else Path(__file__).with_name("archive_sources.json")
    if not source.is_file():
        return []
    raw = json.loads(source.read_text(encoding="utf-8"))
    rows = raw.get("archives") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    return [ArchiveSpec.from_mapping(item) for item in rows if isinstance(item, dict)]


@dataclass
class ArchivePageEvidence:
    status: str
    title: str = ""
    url: str = ""
    crawler: str = ""
    error: str = ""
    links: int = 0
    retrieved_at: str = field(default_factory=now_iso)
    link_rows: list[tuple[str, str]] = field(default_factory=list)


class OfficialArchiveDiscovery:
    """Discover official paper/key documents from configured public archives."""

    def __init__(
        self,
        specs: Iterable[ArchiveSpec] | None = None,
        *,
        browser: Any = None,
        timeout: float = 25.0,
        media_fetch: Callable[[str], Any] | None = None,
        page_fetch: Callable[[str], Any] | None = None,
    ) -> None:
        self.specs = list(specs if specs is not None else load_archive_specs())
        self.browser = browser
        self.timeout = max(5.0, min(float(timeout), 60.0))
        self._media_fetch = media_fetch
        self._page_fetch = page_fetch
        self._media_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._page_cache: dict[str, ArchivePageEvidence] = {}
        self.last_errors: list[str] = []

    def status(self) -> list[dict[str, Any]]:
        return [spec.status() for spec in self.specs]

    def matching_specs(
        self,
        *,
        exam_name: str,
        course_id: str,
        years: Iterable[int | str] = (),
    ) -> list[ArchiveSpec]:
        target = {int(item) for item in years if str(item).strip().isdigit()}
        return [
            spec
            for spec in self.specs
            if spec.year_set & target and self._exam_matches(spec, exam_name, course_id)
        ]

    # -- configuration lookup ---------------------------------------------
    def _subject_codes(self, spec: ArchiveSpec, subject_name: str) -> tuple[str, ...]:
        wanted = _normal(subject_name)
        for subject, codes in sorted(
            spec.subject_codes.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if subject == wanted:
                return codes
        return ()

    def _subject_aliases(
        self, spec: ArchiveSpec, subject_name: str
    ) -> tuple[tuple[str, str], ...]:
        if spec.subject_mode == "all_papers":
            return (("", ""),)
        wanted = _normal(subject_name)
        for subject, aliases in sorted(
            spec.subjects.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if subject == wanted:
                return aliases
        return ()

    def _best_subject_alias(
        self, spec: ArchiveSpec, subject_name: str, hay: str
    ) -> str:
        """Return the most specific configured subject variant, if it matches.

        The archive filename stem is compared, not the whole URL.  A one-token
        alias such as ``Science`` must not match ``Social Science`` or
        ``Data Science``.  The longest matching alias wins.
        """

        wanted = _normal(subject_name)
        best: tuple[int, str, str] | None = None
        for subject, aliases in spec.subjects.items():
            for alias, variant in aliases:
                alias_norm = _normal(alias)
                if not alias_norm:
                    continue
                if alias_norm != hay and not hay.startswith(alias_norm + " "):
                    continue
                score = len(alias_norm.split())
                if best is None or score > best[0]:
                    best = (score, subject, variant or alias)
        if best is None:
            return ""
        _score, subject, variant = best
        if subject == wanted:
            return variant
        return ""

    def _subject_priority(
        self, spec: ArchiveSpec, subject_name: str, matched_variant: str
    ) -> int:
        """Return the configured alias order for a matched subject variant.

        CBSE's English archive contains both Language & Literature and
        Communicative papers.  The order in ``archive_sources.json`` is the
        authority's preference order (for example L&L before Communicative), so
        it must survive candidate sorting rather than being replaced by a
        lexicographic title sort.
        """

        for index, (alias, variant) in enumerate(
            self._subject_aliases(spec, subject_name)
        ):
            if (variant or alias) == matched_variant:
                return index
        return 99

    def _year_sort(self, item: SourceCandidate) -> int:
        year = int((item.evidence.get("identity") or {}).get("year") or 0)
        archive_id = str(item.evidence.get("archive_id") or "")
        spec = next(
            (candidate for candidate in self.specs if candidate.archive_id == archive_id),
            None,
        )
        return year if spec is not None and spec.year_order == "oldest" else -year

    @staticmethod
    def _retrieval_priority(item: SourceCandidate) -> int:
        """Keep the final key and a paper inside small retrieval budgets."""

        variant = str(item.evidence.get("document_variant") or "").casefold()
        if item.document_type == "answer_key" and variant == "final":
            return 0
        if item.document_type in {
            "marking_scheme",
            "question_paper",
            "solved_paper",
        }:
            return 2
        return 3

    def _exam_matches(self, spec: ArchiveSpec, exam_name: str, course_id: str) -> bool:
        text = _normal(f"{exam_name} {course_id}")
        return any(_normal(term) and _normal(term) in text for term in spec.exam_terms)

    # -- page retrieval ----------------------------------------------------
    def _page_evidence(self, url: str) -> ArchivePageEvidence:
        if url in self._page_cache:
            return self._page_cache[url]
        if self._page_fetch is None and self.browser is None:
            evidence = ArchivePageEvidence(status="not_attempted", url=url)
            self._page_cache[url] = evidence
            return evidence
        try:
            if self._page_fetch is not None:
                page = self._page_fetch(url)
            elif self.browser is not None:
                page = self.browser.fetch_document(url)
            evidence = ArchivePageEvidence(
                status="retrieved",
                title=str(getattr(page, "title", "") or ""),
                url=str(getattr(page, "final_url", "") or url),
                crawler=str(getattr(page, "crawler", "") or ""),
                links=len(getattr(page, "links", []) or []),
                link_rows=list(getattr(page, "links", []) or []),
            )
        except Exception as exc:  # noqa: BLE001 - page failure is non-fatal
            evidence = ArchivePageEvidence(
                status="failed",
                url=url,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            self.last_errors.append(f"{url}: {evidence.error}")
        self._page_cache[url] = evidence
        return evidence

    # -- WordPress media provider -----------------------------------------
    def _fetch_media(self, spec: ArchiveSpec, code: str) -> list[dict[str, Any]]:
        key = (spec.archive_id, code)
        if key in self._media_cache:
            return self._media_cache[key]
        url = (
            f"{spec.base_url}{spec.media_api}?per_page={spec.max_media}"
            f"&search={urllib.parse.quote(code)}"
        )
        try:
            if self._media_fetch is not None:
                payload = self._media_fetch(url)
            else:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "ExamEngine/0.2 (local educational archive discovery)",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    body = response.read(8_000_000)
                payload = json.loads(body.decode("utf-8", "replace"))
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
            message = f"{spec.archive_id}/{code}: {type(exc).__name__}: {exc}"[:500]
            self.last_errors.append(message)
            logger.info("official archive media search failed: %s", message)
            self._media_cache[key] = []
            return []
        rows = payload if isinstance(payload, list) else []
        clean = [item for item in rows if isinstance(item, dict)]
        self._media_cache[key] = clean
        return clean

    @staticmethod
    def _title(item: dict[str, Any]) -> str:
        title = item.get("title")
        if isinstance(title, dict):
            title = title.get("rendered") or title.get("raw") or ""
        return str(title or "")

    def _discover_wordpress(
        self,
        spec: ArchiveSpec,
        *,
        course_id: str,
        exam_name: str,
        subject_name: str,
        target_years: set[int],
        query_limit: int,
        seen: set[str],
    ) -> list[SourceCandidate]:
        codes = self._subject_codes(spec, subject_name)
        if not codes:
            return []
        candidates: list[SourceCandidate] = []
        page = self._page_evidence(spec.page_url) if spec.page_url else None
        page_payload = (
            {
                "status": page.status,
                "title": page.title,
                "url": page.url,
                "crawler": page.crawler,
                "error": page.error,
                "links": page.links,
            }
            if page
            else None
        )
        for year in sorted(spec.year_set & target_years):
            for code in codes:
                rows = self._fetch_media(spec, code)
                for item in rows:
                    url = str(item.get("source_url") or "").strip()
                    title = self._title(item).strip()
                    mime = str(item.get("mime_type") or "").casefold()
                    if not url or not url.startswith(("http://", "https://")):
                        continue
                    if mime and mime != "application/pdf":
                        continue
                    if str(year) not in url and str(year)[-2:] not in title:
                        continue
                    lower_title = title.casefold()
                    code_lower = code.casefold()
                    doc_type = ""
                    variant = ""
                    reliability = 0.0
                    reason = ""
                    final_marker = spec.final_key_title.format(code=code).casefold()
                    provisional_marker = spec.provisional_key_title.format(code=code).casefold()
                    paper_marker = spec.paper_title_pattern.format(code=code).casefold()
                    if lower_title.startswith(code_lower) and final_marker in lower_title:
                        doc_type, variant, reliability = "answer_key", "final", 0.999
                        reason = "final_answer_key_filename"
                    elif lower_title.startswith(code_lower) and paper_marker in lower_title:
                        doc_type, variant, reliability = "question_paper", "master", 0.998
                        reason = "master_paper_filename"
                    elif lower_title == provisional_marker or (
                        lower_title.startswith(code_lower)
                        and provisional_marker in lower_title
                        and "final" not in lower_title
                    ):
                        doc_type, variant, reliability = "answer_key", "provisional", 0.97
                        reason = "provisional_answer_key_filename"
                    else:
                        continue
                    if url in seen:
                        continue
                    seen.add(url)
                    session = ""
                    match = re.search(
                        rf"{re.escape(code)}\d{{0,4}}S(\d)", title, re.IGNORECASE
                    )
                    if match:
                        session = match.group(1)
                    identity = {
                        "exam": spec.exam_terms[0].upper() if spec.exam_terms else exam_name,
                        "authority": spec.base_url,
                        "year": year,
                        "subject": subject_name,
                        "subject_code": code,
                        "session": session or (code[-1] if code[-1:].isdigit() else None),
                        "paper": None,
                        "set": code[-1] if code[-1:].isdigit() else None,
                        "class": spec.class_code or None,
                        "language": "English",
                        "document_type": doc_type,
                    }
                    subject_priority = self._subject_priority(
                        spec, subject_name, code
                    )
                    candidates.append(
                        SourceCandidate(
                            source_id="src_" + _sha(f"{course_id}\x1f{url}"),
                            course_id=course_id,
                            title=title or Path(urllib.parse.urlparse(url).path).name,
                            url=url,
                            document_type=doc_type,
                            source_tier=1,
                            reliability=reliability,
                            provider="official_archive",
                            snippet=(
                                f"Official {identity['exam']} {year} {subject_name} "
                                f"{variant} document found in {spec.archive_id}."
                            ),
                            query=f"official archive media:{code}",
                            classification_reason=reason,
                            retrieval_status="discovered",
                            evidence={
                                "archive_id": spec.archive_id,
                                "archive_provider": spec.provider,
                                "archive_page": page_payload,
                                "media_id": item.get("id"),
                                "mime_type": mime or "application/pdf",
                                "identity": identity,
                                "document_variant": variant,
                                "session_ambiguous": len(codes) > 1,
                                "set_ambiguous": False,
                                "matchable": True,
                                "key_availability": spec.key_availability,
                                "year_order": spec.year_order,
                                "subject_priority": subject_priority,
                                "snippet_is_not_answer_evidence": True,
                                "discovered_at": now_iso(),
                            },
                        )
                    )
        return candidates

    # -- HTML archive provider --------------------------------------------
    @staticmethod
    def _document_kind(url: str, label: str) -> str:
        text = f"{urllib.parse.urlparse(url).path} {label}".casefold()
        if "marking-scheme" in text or "marking scheme" in text:
            return "marking_scheme"
        if "model-answer" in text or "model answer" in text:
            return "model_answer"
        if "question-paper" in text or "past_qps" in text or "question paper" in text:
            return "question_paper"
        if "answer" in text and "key" in text:
            return "answer_key"
        return "other"

    def _discover_html(
        self,
        spec: ArchiveSpec,
        *,
        course_id: str,
        exam_name: str,
        subject_name: str,
        target_years: set[int],
        sets: Iterable[str],
        query_limit: int,
        seen: set[str],
    ) -> list[SourceCandidate]:
        aliases = self._subject_aliases(spec, subject_name)
        if not aliases:
            return []
        set_values = [str(item) for item in sets if str(item).strip()]
        preferred_set = sorted(set_values)[0] if set_values else ""
        candidates: list[SourceCandidate] = []
        for year in sorted(spec.year_set & target_years):
            for page_url in spec.page_urls:
                page = self._page_evidence(page_url)
                page_payload = {
                    "status": page.status,
                    "title": page.title,
                    "url": page.url,
                    "crawler": page.crawler,
                    "error": page.error,
                    "links": page.links,
                }
                if page.status != "retrieved":
                    continue
                for raw_url, label in page.link_rows:
                    url = str(raw_url or "").strip()
                    path = urllib.parse.urlparse(url).path
                    suffix = Path(path).suffix.casefold()
                    if suffix not in _DOCUMENT_SUFFIXES:
                        continue
                    hay = _normal(f"{path} {label}")
                    if any(
                        term in hay
                        for term in ("comptt", "compartment", "supplementary", "aat")
                    ):
                        continue
                    if str(year) not in path and str(year) not in label:
                        continue
                    if spec.class_code and f"/{spec.class_code.casefold()}/" not in path.casefold():
                        continue
                    if spec.language and "hindi" in hay and "english" not in hay:
                        continue
                    kind = self._document_kind(url, label)
                    if kind not in {"question_paper", "marking_scheme"}:
                        continue
                    matched_variant = ""
                    if spec.subject_mode != "all_papers":
                        stem = Path(path).stem
                        stem = re.sub(r"^\d{2,3}[_\- ]+", "", stem)
                        matched_variant = self._best_subject_alias(
                            spec, subject_name, _normal(stem)
                        )
                        if not matched_variant:
                            continue
                    paper = ""
                    paper_match = re.search(r"(?:^|/)(?:past_qps/)?(20\d{2})_([12])(?:_|\.)", path)
                    if paper_match:
                        paper = paper_match.group(2)
                    else:
                        paper_match = re.search(r"(?:paper|p)[-_ ]?([12])(?:_|\.|$)", path, re.I)
                        if paper_match:
                            paper = paper_match.group(1)
                    set_value = ""
                    set_ambiguous = False
                    bundle = suffix == ".zip"
                    if spec.set_aware:
                        set_value = preferred_set
                        if set_value:
                            url = f"{url}#set={set_value}"
                        else:
                            url = f"{url}#set=all"
                            set_ambiguous = True
                    if url in seen:
                        continue
                    seen.add(url)
                    identity = {
                        "exam": spec.exam_terms[0].upper() if spec.exam_terms else exam_name,
                        "authority": urllib.parse.urlparse(url).netloc,
                        "year": year,
                        "subject": subject_name,
                        "subject_code": None,
                        "subject_variant": matched_variant or None,
                        "session": paper or None,
                        "paper": paper or None,
                        "set": set_value or None,
                        "class": spec.class_code or None,
                        "language": spec.language or "English",
                        "document_type": kind,
                    }
                    subject_priority = self._subject_priority(
                        spec, subject_name, matched_variant
                    )
                    variant = (
                        "marking_scheme" if kind == "marking_scheme" else "master"
                    )
                    candidates.append(
                        SourceCandidate(
                            source_id="src_" + _sha(f"{course_id}\x1f{url}"),
                            course_id=course_id,
                            title=label.strip() or Path(path).name,
                            url=url,
                            document_type=kind,
                            source_tier=1,
                            reliability=0.999 if kind == "question_paper" else 0.995,
                            provider="official_archive",
                            snippet=(
                                f"Official {identity['exam']} {year} {subject_name} "
                                f"{kind.replace('_', ' ')} found in {spec.archive_id}."
                            ),
                            query=f"official archive page:{spec.archive_id}",
                            classification_reason=f"official_{kind}_link",
                            retrieval_status="discovered",
                            evidence={
                                "archive_id": spec.archive_id,
                                "archive_provider": spec.provider,
                                "archive_page": page_payload,
                                "identity": identity,
                                "document_variant": variant,
                                "session_ambiguous": set_ambiguous,
                                "set_ambiguous": set_ambiguous,
                                "bundle": bundle,
                                "matchable": spec.matchable,
                                "key_availability": spec.key_availability,
                                "year_order": spec.year_order,
                                "subject_priority": subject_priority,
                                "snippet_is_not_answer_evidence": True,
                                "discovered_at": now_iso(),
                            },
                        )
                    )
        return candidates

    # -- explicit document URLs -------------------------------------------
    def _discover_documents(
        self,
        spec: ArchiveSpec,
        *,
        course_id: str,
        exam_name: str,
        subject_name: str,
        target_years: set[int],
        seen: set[str],
    ) -> list[SourceCandidate]:
        candidates: list[SourceCandidate] = []
        for document in spec.documents:
            url = str(document.get("url") or "").strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            doc_year = int(document.get("year") or spec.year or 0)
            if doc_year and doc_year not in target_years:
                continue
            subject = str(document.get("subject") or "").strip()
            if subject and _normal(subject) not in _normal(subject_name):
                continue
            seen.add(url)
            identity = dict(document.get("identity") or {})
            identity.setdefault("exam", spec.exam_terms[0].upper() if spec.exam_terms else exam_name)
            identity.setdefault("year", doc_year)
            identity.setdefault("subject", subject_name)
            kind = str(document.get("document_type") or "answer_key")
            candidates.append(
                SourceCandidate(
                    source_id="src_" + _sha(f"{course_id}\x1f{url}"),
                    course_id=course_id,
                    title=str(document.get("title") or Path(urllib.parse.urlparse(url).path).name),
                    url=url,
                    document_type=kind,
                    source_tier=1,
                    reliability=float(document.get("reliability") or 0.99),
                    provider="official_archive",
                    snippet=str(document.get("note") or f"Configured official {kind} document."),
                    query=f"official archive document:{spec.archive_id}",
                    classification_reason="configured_official_document",
                    retrieval_status="discovered",
                    evidence={
                        "archive_id": spec.archive_id,
                        "archive_provider": spec.provider,
                        "identity": identity,
                        "document_variant": str(document.get("variant") or "final"),
                        "session_ambiguous": False,
                        "set_ambiguous": False,
                        "matchable": bool(document.get("matchable", spec.matchable)),
                        "unmatchable_reason": document.get("unmatchable_reason")
                        or spec.unavailable_reason,
                        "key_availability": spec.key_availability,
                        "year_order": spec.year_order,
                        "snippet_is_not_answer_evidence": True,
                        "discovered_at": now_iso(),
                    },
                )
            )
        return candidates

    def discover(
        self,
        *,
        course_id: str,
        exam_name: str,
        subject_name: str,
        years: Iterable[int | str] = (),
        paper_labels: Iterable[str] = (),
        sets: Iterable[str] = (),
        query_limit: int = 3,
    ) -> list[SourceCandidate]:
        del paper_labels  # identity is established from the archive page and record IDs
        target_years = {int(item) for item in years if str(item).strip().isdigit()}
        if not target_years:
            return []
        candidates: list[SourceCandidate] = []
        seen: set[str] = set()
        for spec in self.specs:
            if not spec.available:
                continue
            if not spec.year_set & target_years:
                continue
            if not self._exam_matches(spec, exam_name, course_id):
                continue
            if spec.provider == "wordpress_media":
                candidates.extend(
                    self._discover_wordpress(
                        spec,
                        course_id=course_id,
                        exam_name=exam_name,
                        subject_name=subject_name,
                        target_years=target_years,
                        query_limit=query_limit,
                        seen=seen,
                    )
                )
            elif spec.provider == "html_archive":
                candidates.extend(
                    self._discover_html(
                        spec,
                        course_id=course_id,
                        exam_name=exam_name,
                        subject_name=subject_name,
                        target_years=target_years,
                        sets=sets,
                        query_limit=query_limit,
                        seen=seen,
                    )
                )
            elif spec.provider == "document_urls":
                candidates.extend(
                    self._discover_documents(
                        spec,
                        course_id=course_id,
                        exam_name=exam_name,
                        subject_name=subject_name,
                        target_years=target_years,
                        seen=seen,
                    )
                )
        candidates.sort(
            key=lambda item: (
                item.source_tier,
                self._retrieval_priority(item),
                -item.reliability,
                self._year_sort(item),
                item.evidence.get("document_variant") != "final",
                item.title.casefold(),
                item.url,
            )
        )
        return candidates[: max(1, min(int(query_limit) * 16, 48))]


__all__ = [
    "ArchivePageEvidence",
    "ArchiveSpec",
    "OfficialArchiveDiscovery",
    "load_archive_specs",
]
