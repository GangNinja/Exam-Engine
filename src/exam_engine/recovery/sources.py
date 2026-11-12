"""Reusable source discovery and answer-key matching.

The engine never treats a search snippet as an answer. This module classifies
discovered documents, keeps their provenance, and can match explicitly parsed
answer-key entries to records. Network retrieval is optional and conservative:
only public HTTP(S) documents are read, size is bounded, and callers can restrict
hosts. Authentication, paywalls, CAPTCHAs and robots restrictions are not
bypassed.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from exam_engine.models.recovery import SourceCandidate, now_iso

DOCUMENT_TYPES = (
    "question_paper",
    "answer_key",
    "official_solution",
    "marking_scheme",
    "solved_paper",
    "syllabus",
    "other",
)

_QUESTION_PAPER_TERMS = (
    "question paper",
    "question papers",
    "question-paper",
    "question-papers",
    "questionpaper",
    "master question paper",
    "master question papers",
    "previous year paper",
    "previous-year paper",
    "past paper",
    "exam paper",
    "examination paper",
    "pyq",
)
_ANSWER_KEY_TERMS = (
    "final answer key",
    "final answer keys",
    "answer key",
    "answer keys",
    "answer-key",
    "answer-keys",
    "answerkey",
    "official key",
    "official answer key",
    "key pdf",
)
_SOLUTION_TERMS = ("official solution", "official solutions")
_MARKING_TERMS = ("marking scheme", "marking-scheme", "evaluation scheme")
_SOLVED_TERMS = ("solved paper", "solved papers", "solution pdf", "solutions pdf")
_SYLLABUS_TERMS = ("syllabus", "curriculum", "course structure")

_OFFICIAL_SUFFIXES = (
    ".gov.in",
    ".nic.in",
    ".ac.in",
    "nta.ac.in",
    "gate.iit",
    "iisc.ac.in",
    "iitm.ac.in",
    "iitb.ac.in",
    "iitd.ac.in",
    "iitg.ac.in",
    "iitr.ac.in",
    "iitk.ac.in",
    "iitkgp.ac.in",
    "cbse.gov.in",
    "nta.nic.in",
    "jeeadv.ac.in",
    "ntaexam.ac.in",
)
_TRUSTED_SUFFIXES = (
    ".edu",
    "nptel.ac.in",
    "swayam.gov.in",
    "khanacademy.org",
    "mit.edu",
    "stanford.edu",
    "arxiv.org",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_KEY_LINE_RE = re.compile(
    r"(?im)^\s*(?:Q(?:uestion)?\s*)?(?:No\.?\s*)?(\d{1,3})\s*[.)\-:|]?\s*"
    r"(?:\(?([A-Ha-h])\)?|([+-]?\d+(?:\.\d+)?)|(true|false))\s*$"
)
_KEY_INLINE_RE = re.compile(
    r"(?i)(?:^|[\s;|])(?:Q(?:uestion)?\s*)?(\d{1,3})\s*[.)\-:|]\s*"
    r"(?:\(?([A-Ha-h])\)?|([+-]?\d+(?:\.\d+)?))(?=[\s;|]|$)"
)
_KEY_TABLE_RE = re.compile(
    r"(?im)^\s*(?:Q(?:uestion)?\s*)?(?:No\.?\s*)?(\d{1,3})\s*[|,]\s*"
    r"(?:\(?([A-Ha-h])\)?|([+-]?\d+(?:\.\d+)?)|(true|false))\s*$"
)


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int | None = None) -> list[Any]: ...


def _clean(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value or ""))).strip()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:20]


def source_tier_for_url(url: str) -> int:
    """Return 1 for official, 2 for trusted educational, otherwise 3."""

    lowered = (url or "").casefold()
    host = urllib.parse.urlparse(lowered).netloc
    target = host or lowered
    if any(suffix in target for suffix in _OFFICIAL_SUFFIXES):
        return 1
    if any(suffix in target for suffix in _TRUSTED_SUFFIXES):
        return 2
    return 3


def classify_source(
    *,
    title: str = "",
    url: str = "",
    snippet: str = "",
    filename: str = "",
) -> tuple[str, int, float, str]:
    """Classify a public document using explicit textual evidence only.

    ``paper`` alone is intentionally insufficient: a syllabus named
    ``paper-syllabus.pdf`` must never become a question paper.
    """

    text = f"{title} {filename} {snippet} {url}".casefold()
    tier = source_tier_for_url(url)
    if any(term in text for term in _SYLLABUS_TERMS):
        return "syllabus", tier, 0.25, "syllabus_or_curriculum_term"
    if any(term in text for term in _ANSWER_KEY_TERMS):
        return "answer_key", tier, 0.95 if tier == 1 else 0.75, "explicit_answer_key_term"
    if any(term in text for term in _MARKING_TERMS):
        return "marking_scheme", tier, 0.9 if tier == 1 else 0.7, "explicit_marking_scheme_term"
    if any(term in text for term in _SOLUTION_TERMS):
        return "official_solution", tier, 0.9 if tier == 1 else 0.65, "explicit_official_solution_term"
    if any(term in text for term in _SOLVED_TERMS):
        return "solved_paper", tier, 0.55, "explicit_solved_paper_term"
    if any(term in text for term in _QUESTION_PAPER_TERMS):
        return "question_paper", tier, 0.9 if tier == 1 else 0.65, "explicit_question_paper_term"
    return "other", tier, 0.2, "no_document_type_evidence"


def build_search_queries(
    *,
    exam_name: str,
    subject_name: str,
    years: Iterable[int | str] = (),
    paper_labels: Iterable[str] = (),
    limit: int = 3,
) -> list[str]:
    """Generate a small, examination-agnostic set of discovery queries."""

    exam = _clean(exam_name)
    subject = _clean(subject_name)
    # Prefer the most recent printed year. Sorting lexical strings would query
    # an old paper first and hide the current official archive from providers.
    raw_years = [str(item).strip() for item in years if str(item).strip()]
    raw_years.sort(
        key=lambda value: int(value) if value.isdigit() else -1,
        reverse=True,
    )
    year_values = raw_years[:2]
    paper_values = [_clean(str(item)) for item in paper_labels if str(item).strip()][:2]
    base_parts = [part for part in (exam, subject) if part]
    # One identifying attribute is enough. Adding every year and paper code
    # makes the query so narrow that public providers return no results.
    if year_values:
        base_parts.append(year_values[0])
    elif paper_values:
        base_parts.append(paper_values[0])
    base = " ".join(base_parts)
    if not base:
        return []
    # Avoid provider-specific operators such as ``filetype:`` in the first
    # query: the no-key fallback provider does not understand them and would
    # silently return zero results.
    queries = [
        f'"{base}" official question paper answer key',
        f'"{base}" official final answer key pdf',
        f'"{base}" official solutions marking scheme',
        f'"{base}" previous year question paper pdf',
    ]
    if not year_values:
        queries.insert(1, f'"{base}" previous year question paper pdf')
    return queries[: max(0, min(limit, len(queries)))]


class SourceDiscovery:
    """Discover and classify sources without accepting snippets as answers."""

    def __init__(
        self,
        search: SearchProvider | Callable[..., list[Any]] | None,
        *,
        result_limit: int = 3,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        self.search = search
        self.result_limit = max(1, min(result_limit, 6))
        self.allowed_hosts = tuple(host.casefold() for host in (allowed_hosts or ()))

    def discover(
        self,
        *,
        course_id: str,
        exam_name: str,
        subject_name: str,
        years: Iterable[int | str] = (),
        paper_labels: Iterable[str] = (),
        query_limit: int = 3,
    ) -> list[SourceCandidate]:
        if self.search is None:
            return []
        queries = build_search_queries(
            exam_name=exam_name,
            subject_name=subject_name,
            years=years,
            paper_labels=paper_labels,
            limit=query_limit,
        )
        candidates: list[SourceCandidate] = []
        seen: set[str] = set()
        for query in queries:
            try:
                results = (
                    self.search.search(query, limit=self.result_limit)
                    if hasattr(self.search, "search")
                    else self.search(query, limit=self.result_limit)  # type: ignore[operator]
                )
            except (OSError, TimeoutError, ValueError, urllib.error.URLError):
                continue
            except Exception:  # noqa: BLE001 - optional external retrieval
                continue
            for item in results or []:
                title = str(getattr(item, "title", "") or (item.get("title") if isinstance(item, dict) else "") or "").strip()
                url = str(getattr(item, "url", "") or (item.get("url") if isinstance(item, dict) else "") or "").strip()
                snippet = str(getattr(item, "snippet", "") or (item.get("snippet") if isinstance(item, dict) else "") or "")
                provider = str(getattr(item, "provider", "") or (item.get("provider") if isinstance(item, dict) else "") or "unknown")
                if not url or url in seen:
                    continue
                if self.allowed_hosts:
                    host = urllib.parse.urlparse(url).netloc.casefold()
                    if not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts):
                        continue
                kind, tier, reliability, reason = classify_source(
                    title=title, url=url, snippet=snippet
                )
                seen.add(url)
                candidates.append(
                    SourceCandidate(
                        source_id="src_" + _sha(f"{course_id}\x1f{url}"),
                        course_id=course_id,
                        title=title or url,
                        url=url,
                        document_type=kind,
                        source_tier=tier,
                        reliability=reliability,
                        provider=provider,
                        snippet=_clean(snippet)[:1200],
                        query=query,
                        classification_reason=reason,
                        retrieval_status="discovered" if kind != "other" else "unclassified",
                        evidence={
                            "tier_definition": {
                                1: "official examination authority or institution",
                                2: "trusted educational source",
                                3: "other public source",
                            }[tier],
                            "snippet_is_not_answer_evidence": True,
                        },
                    )
                )
        candidates.sort(key=lambda item: (item.source_tier, -item.reliability, item.title.casefold()))
        return candidates


@dataclass(frozen=True)
class AnswerKeyEntry:
    question_number: str
    answer: str
    source_line: str = ""
    source_kind: str = "answer_key"
    metadata: dict[str, Any] = field(default_factory=dict)


class AnswerKeyIndex:
    """Explicit question-number/answer mapping parsed from a source document."""

    def __init__(self) -> None:
        self._entries: dict[str, list[AnswerKeyEntry]] = {}

    @staticmethod
    def normalize_number(value: str | int | None) -> str:
        text = str(value or "").strip().casefold()
        text = re.sub(r"^(?:q(?:uestion)?\s*)", "", text)
        text = re.sub(r"[^0-9a-z]+$", "", text)
        match = re.search(r"\d{1,3}", text)
        if match:
            return str(int(match.group()))
        return text

    def add(self, entry: AnswerKeyEntry) -> None:
        number = self.normalize_number(entry.question_number)
        if not number or not entry.answer:
            return
        self._entries.setdefault(number, []).append(entry)

    def extend(self, entries: Iterable[AnswerKeyEntry]) -> None:
        for entry in entries:
            self.add(entry)

    def match(self, question_number: str | int | None) -> list[AnswerKeyEntry]:
        return list(self._entries.get(self.normalize_number(question_number), ()))

    def __len__(self) -> int:
        return sum(len(items) for items in self._entries.values())

    def as_dict(self) -> dict[str, list[str]]:
        return {
            number: [entry.answer for entry in entries]
            for number, entries in self._entries.items()
        }


def _extract_tabular_key_entries(text: str) -> list[AnswerKeyEntry]:
    """Parse multi-column official key tables (e.g. GATE's six-column PDF).

    The table is emitted by PDF extraction as one cell per line.  Requiring an
    explicit header and a run of structurally valid rows prevents ordinary prose
    tables from becoming answer evidence.
    """

    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    header_index: int | None = None
    for index, line in enumerate(lines):
        window = {item.casefold().rstrip(".") for item in lines[index : index + 8]}
        normalized = line.casefold().rstrip(".")
        is_header = normalized in {
            "q no",
            "q. no",
            "question no",
            "question number",
        } or normalized.startswith(("q no", "q. no", "question no"))
        if is_header and any(
            "key" in item or "session" in item or "mark" in item for item in window
        ):
            header_index = index + 1
            break
    if header_index is None:
        return []
    entries: list[AnswerKeyEntry] = []
    index = header_index
    variant = "final" if "final" in text.casefold() else "provisional" if "provisional" in text.casefold() else ""
    while index + 5 < len(lines):
        q_no, session, q_type, section, key_value, marks = lines[index : index + 6]
        if not (
            re.fullmatch(r"\d{1,3}", q_no)
            and re.fullmatch(r"\d+", session)
            and q_type.upper() in {"MCQ", "MSQ", "NAT"}
        ):
            index += 1
            continue
        answer = " ".join(key_value.split()).strip()
        if not answer or answer in {"-", "--"}:
            index += 6
            continue
        if q_type.upper() in {"MCQ", "MSQ"}:
            answer = answer.upper().replace(",", ";")
        entries.append(
            AnswerKeyEntry(
                question_number=q_no,
                answer=answer,
                source_line=" | ".join(lines[index : index + 6])[:300],
                source_kind="tabular_answer_key",
                metadata={
                    "session": session,
                    "question_type": q_type.upper(),
                    "section": section,
                    "marks": marks,
                    "key_variant": variant,
                    "marks_to_all": answer.upper().rstrip("*").strip() == "MTA",
                },
            )
        )
        index += 6
    return entries if len(entries) >= 3 else []


def extract_answer_key_entries(text: str | None) -> list[AnswerKeyEntry]:
    """Parse only explicit key lines; ambiguous prose is ignored."""

    if not text:
        return []
    tabular = _extract_tabular_key_entries(text)
    if tabular:
        return tabular
    entries: list[AnswerKeyEntry] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = _clean(line)
        if not stripped:
            continue
        match = _KEY_LINE_RE.match(stripped) or _KEY_TABLE_RE.match(stripped)
        if match:
            answer = next((part for part in match.groups()[1:] if part), "")
            if answer:
                key = (AnswerKeyIndex.normalize_number(match.group(1)), answer.upper() if len(answer) == 1 else answer)
                if key not in seen:
                    seen.add(key)
                    entries.append(
                        AnswerKeyEntry(
                            question_number=match.group(1),
                            answer=key[1],
                            source_line=stripped[:240],
                        )
                    )
    if entries:
        return entries
    # Some keys are published as one line: "1-B 2-C 3-A". Only use this fallback
    # when no line-oriented entry exists and the text is explicitly key-like.
    if re.search(r"(?i)\b(?:answer\s*key|key)\b", text):
        for match in _KEY_INLINE_RE.finditer(text):
            answer = match.group(2) or match.group(3) or ""
            if not answer:
                continue
            number = AnswerKeyIndex.normalize_number(match.group(1))
            answer = answer.upper() if len(answer) == 1 else answer
            if (number, answer) not in seen:
                seen.add((number, answer))
                entries.append(
                    AnswerKeyEntry(
                        question_number=match.group(1),
                        answer=answer,
                        source_line=match.group(0).strip()[:240],
                    )
                )
    return entries


def extract_cbse_marking_scheme_entries(text: str | None) -> list[AnswerKeyEntry]:
    """Extract only explicit MCQ answers from a CBSE marking scheme.

    CBSE PDFs use several layouts: ``1. (B) text``, a bare question number
    followed by ``(B)``, compact key tables with a bare letter, and a ``Sol.``
    line between the number and the option.  Only Section A contains objective
    answers; descriptive value points must never become whole-question keys.

    A marking-scheme bundle can contain several paper sets.  Parse the first
    explicit Section A only and stop when another Section A starts.  This keeps
    the entries attributable to the set selected by the archive fragment (the
    first section is the selected set), instead of mixing duplicate question
    numbers from sets 2 and 3.
    """

    if not text:
        return []
    raw = [line.strip() for line in text.splitlines()]
    lines = [_clean(line) for line in raw]
    entries: list[AnswerKeyEntry] = []
    seen: set[str] = set()

    def add(number: str, option: str, source_line: str) -> None:
        number = str(int(number))
        option = option.upper()
        # A question can occur only once in the selected set.  If a duplicate
        # appears, it belongs to a later set or a descriptive alternative and
        # must not become a second official answer.
        if number in seen:
            return
        seen.add(number)
        entries.append(
            AnswerKeyEntry(
                question_number=number,
                answer=option,
                source_line=source_line[:240],
                source_kind="cbse_marking_scheme",
                metadata={
                    "key_variant": "marking_scheme",
                    "question_type": "MCQ",
                    "answer_scope": "mcq_only",
                },
            )
        )

    def next_answer(start: int, stop: int = 12) -> tuple[int, str] | None:
        for cursor in range(start, min(start + stop, len(raw))):
            candidate = lines[cursor]
            if not candidate:
                continue
            if re.fullmatch(r"(?:sol(?:ution)?|ans(?:wer)?)\.?", candidate, re.IGNORECASE):
                continue
            return cursor, candidate
        return None

    section_a = False
    section_a_seen = False
    for index, line in enumerate(lines):
        if re.match(r"^section\s+a\b", line, re.IGNORECASE):
            if section_a_seen and entries:
                break
            section_a = True
            section_a_seen = True
            continue
        if re.match(r"^section\s+(?!a\b)[b-z]\b", line, re.IGNORECASE):
            section_a = False
            continue
        if not section_a:
            continue

        inline = re.match(
            r"^(\d{1,2})\s*[.)]\s*\(?\s*([A-Da-d])\s*\)?\s*(.*)$", line
        )
        if inline and 1 <= int(inline.group(1)) <= 100:
            add(inline.group(1), inline.group(2), line)
            continue

        marker = re.fullmatch(r"(?:q\.?\s*)?(\d{1,2})\s*[.)]?", line, re.IGNORECASE)
        if not marker or not 1 <= int(marker.group(1)) <= 100:
            continue
        found = next_answer(index + 1)
        if found is None:
            continue
        _cursor, candidate = found
        option_match = re.match(
            r"^\(?\s*([A-Da-d])\s*\)?(?:\s+|$)", candidate
        )
        if not option_match:
            continue
        add(marker.group(1), option_match.group(1), f"{line} {candidate}")

    return entries if len(entries) >= 5 else []


class HttpDocumentFetcher:
    """Small, conservative fetcher for explicitly permitted public documents."""

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str] | None = None,
        max_bytes: int = 4_000_000,
        timeout: float = 10.0,
    ) -> None:
        self.allowed_hosts = tuple(host.casefold() for host in (allowed_hosts or ()))
        self.max_bytes = max(1024, min(max_bytes, 8_000_000))
        self.timeout = timeout

    def _validate(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("only public http(s) documents can be retrieved")
        host = parsed.hostname or ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("private/loopback document hosts are not permitted")
        if host.casefold() in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("local document hosts are not permitted")
        if self.allowed_hosts and not any(
            host.casefold() == allowed or host.casefold().endswith("." + allowed)
            for allowed in self.allowed_hosts
        ):
            raise ValueError("document host is not in the permitted allowlist")
        return url

    def fetch(self, url: str) -> tuple[str, str]:
        """Return ``(content_type, extracted_text)`` for a permitted document."""

        url = self._validate(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ExamEngine/0.1 (local educational verification; contact: local user)",
                "Accept": "application/pdf,text/html,text/plain",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].casefold()
            payload = response.read(self.max_bytes + 1)
        if len(payload) > self.max_bytes:
            raise ValueError("document exceeds the configured retrieval size")
        if "pdf" in content_type or payload[:4] == b"%PDF":
            try:
                import fitz  # type: ignore

                document = fitz.open(stream=payload, filetype="pdf")
                try:
                    text = "\n".join(
                        page.get_text("text") for page in document
                    )
                finally:
                    document.close()
            except Exception as exc:  # noqa: BLE001 - report extraction limitation
                raise ValueError(f"PDF extraction failed: {type(exc).__name__}") from exc
            return "application/pdf", text
        if content_type in {"text/html", "application/xhtml+xml"} or payload.lstrip()[:1] == b"<":
            return "text/html", _clean(payload.decode("utf-8", "replace"))
        if content_type in {"text/plain", ""}:
            return "text/plain", payload.decode("utf-8", "replace")
        raise ValueError(f"unsupported document content type: {content_type or 'unknown'}")


def source_counts(candidates: Iterable[SourceCandidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.document_type] = counts.get(candidate.document_type, 0) + 1
    return counts


__all__ = [
    "AnswerKeyEntry",
    "AnswerKeyIndex",
    "DOCUMENT_TYPES",
    "HttpDocumentFetcher",
    "SourceDiscovery",
    "build_search_queries",
    "classify_source",
    "extract_cbse_marking_scheme_entries",
    "extract_answer_key_entries",
    "source_counts",
    "source_tier_for_url",
]
