"""Examination dataset discovery for the personal GATE/CBSE/JEE/NEET corpus.

The dataset folders are the source of truth. Nothing here rewrites them: the
catalog records which files exist and how the existing folder hierarchy maps to
course-scoped records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_GATE_CODES: dict[str, str] = {
    "aerospace engineering": "AE",
    "chemistry": "CY",
    "civil engineering": "CE",
    "computer science and information technology": "CS",
    "electrical engineering": "EE",
    "electronics and communication engineering": "EC",
    "engineering sciences": "XE",
    "mathematics": "MA",
    "physics": "PH",
    "statistics": "ST",
}

_SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "maths": ("maths", "mathematics"),
    "mathematics": ("mathematics", "maths"),
    "it": ("information technology", "it"),
    "english": ("english", "english core"),
    "social science": ("social science",),
    "science": ("science",),
}


@dataclass(frozen=True)
class ExamCategory:
    category_id: str
    name: str
    institution_code: str
    institution_name: str
    programme: str
    root: Path


@dataclass(frozen=True)
class ExamSubject:
    category: ExamCategory
    subject_name: str
    subject_slug: str
    course_id: str
    course_code: str
    csv_path: Path
    syllabus_paths: tuple[Path, ...]
    syllabus_version: str | None

    @property
    def category_id(self) -> str:
        return self.category.category_id


@dataclass(frozen=True)
class DatasetCatalog:
    root: Path
    categories: tuple[ExamCategory, ...]
    subjects: tuple[ExamSubject, ...]

    @property
    def csv_files(self) -> tuple[Path, ...]:
        return tuple(subject.csv_path for subject in self.subjects)

    @property
    def syllabus_files(self) -> tuple[Path, ...]:
        return tuple(path for subject in self.subjects for path in subject.syllabus_paths)

    def subject(self, course_id: str) -> ExamSubject | None:
        return next((item for item in self.subjects if item.course_id == course_id), None)

    def subjects_for_category(self, category_id: str) -> tuple[ExamSubject, ...]:
        return tuple(item for item in self.subjects if item.category_id == category_id)


def slugify(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = _SLUG_RE.sub("-", text).strip("-")
    return re.sub(r"-{2,}", "-", text)


def clean_subject_name(value: str) -> str:
    text = value.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("and Communication", "and Communication")
    return text


def _category_specs(root: Path) -> list[tuple[Path, str, str, str]]:
    return [
        (root / "GATE", "gate", "GATE", "GATE"),
        (root / "BOARD EXAMS" / "class 10", "cbse-class-10", "CBSE", "CBSE Class 10"),
        (root / "BOARD EXAMS" / "class 12", "cbse-class-12", "CBSE", "CBSE Class 12"),
        (root / "JEE Advanced", "jee-advanced", "JEE Advanced", "JEE Advanced"),
        (root / "JEE Mains", "jee-mains", "JEE Mains", "JEE Mains"),
        (root / "NEET", "neet", "NEET", "NEET"),
    ]


def _syllabus_year(paths: tuple[Path, ...]) -> str | None:
    for path in paths:
        match = re.search(r"(20\d{2})(?:[-–](?:20)?(\d{2}))?", path.name)
        if match:
            suffix = match.group(2)
            return f"{match.group(1)}-{suffix}" if suffix else match.group(1)
    return None


def _matches_syllabus(subject_name: str, path: Path) -> bool:
    subject_tokens = set(re.findall(r"[a-z0-9]+", subject_name.lower()))
    aliases = _SUBJECT_ALIASES.get(subject_name.lower(), (subject_name,))
    file_tokens = set(re.findall(r"[a-z0-9]+", path.name.lower()))
    for alias in aliases:
        alias_tokens = set(re.findall(r"[a-z0-9]+", alias.lower()))
        if alias_tokens and alias_tokens <= file_tokens:
            if subject_name.lower() == "science" and "social" in file_tokens:
                continue
            return True
    if subject_tokens and subject_tokens <= file_tokens:
        return subject_name.lower() != "science" or "social" not in file_tokens
    return False


def discover(root: Path | str = Path("datasets")) -> DatasetCatalog:
    """Discover every supported examination CSV and syllabus PDF."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"dataset root not found: {root_path}")

    categories: list[ExamCategory] = []
    subjects: list[ExamSubject] = []
    for directory, category_id, institution_code, programme in _category_specs(root_path):
        if not directory.is_dir():
            continue
        category = ExamCategory(
            category_id=category_id,
            name=programme,
            institution_code=institution_code.lower(),
            institution_name=programme,
            programme=programme,
            root=directory,
        )
        categories.append(category)
        syllabus_dir = directory / "Syllabus"
        pdfs = tuple(sorted(syllabus_dir.glob("*.pdf"))) if syllabus_dir.is_dir() else ()
        for csv_path in sorted(directory.glob("*.csv")):
            subject_name = clean_subject_name(csv_path.stem)
            subject_slug = slugify(subject_name)
            if category_id == "gate":
                code = _GATE_CODES.get(subject_name.lower())
                if not code:
                    raise ValueError(
                        f"GATE subject '{subject_name}' has no recognised subject code; "
                        "refusing to invent one"
                    )
                course_code = code
                matched = tuple(path for path in pdfs if path.name.upper().startswith(f"{code}_"))
            else:
                prefix = {
                    "cbse-class-10": "CBSE10",
                    "cbse-class-12": "CBSE12",
                    "jee-advanced": "JEEA",
                    "jee-mains": "JEEM",
                    "neet": "NEET",
                }[category_id]
                course_code = f"{prefix}-{subject_name.upper().replace(' ', '-')}"[:32]
                matched = tuple(path for path in pdfs if _matches_syllabus(subject_name, path))
            subjects.append(
                ExamSubject(
                    category=category,
                    subject_name=subject_name,
                    subject_slug=subject_slug,
                    course_id=f"{category_id}-{subject_slug}"[:80].rstrip("-"),
                    course_code=course_code,
                    csv_path=csv_path,
                    syllabus_paths=matched,
                    syllabus_version=_syllabus_year(matched),
                )
            )

    return DatasetCatalog(
        root=root_path,
        categories=tuple(categories),
        subjects=tuple(subjects),
    )


__all__ = [
    "DatasetCatalog",
    "ExamCategory",
    "ExamSubject",
    "clean_subject_name",
    "discover",
    "slugify",
]
