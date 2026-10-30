from __future__ import annotations

from exam_engine.models.enums import NlpMethod, TextOwnerType
from exam_engine.nlp.entities import (
    RawEntity,
    RuleBasedEntityExtractor,
    SpacyEntityExtractor,
    build_extractor,
    entity_records,
    normalize_entity,
)

TEXT = (
    "Prof. Rao taught Algorithm 4 at the Indian Institute of Technology. "
    "In 2019 the class compared O(n log n) sorting with TCP congestion control. "
    "The quicksort algorithm partitions the array."
)


def test_rule_based_extractor_finds_expected_labels() -> None:
    labels = {entity.label for entity in RuleBasedEntityExtractor().extract(TEXT)}
    assert {"PERSON", "METHOD", "ORG", "DATE", "NOTATION", "ACRONYM"} <= labels


def test_entities_never_cross_a_line_break() -> None:
    text = "Algorithms\nDemo Institute of Technology\nAcademic Year 2024-25"
    entities = RuleBasedEntityExtractor().extract(text)
    assert any(entity.text == "Demo Institute" for entity in entities)
    assert all("\n" not in entity.text for entity in entities)
    for entity in entities:
        assert "\n" not in text[entity.start : entity.end]


def test_rule_based_spans_point_into_the_text() -> None:
    for entity in RuleBasedEntityExtractor().extract(TEXT):
        assert TEXT[entity.start : entity.end].strip() == entity.text


def test_rule_based_drops_overlapping_spans() -> None:
    spans = [(entity.start, entity.end) for entity in RuleBasedEntityExtractor().extract(TEXT)]
    for index, (start, end) in enumerate(spans):
        for other_start, other_end in spans[index + 1 :]:
            assert end <= other_start or start >= other_end


def test_rule_based_never_claims_verified_concepts() -> None:
    extractor = RuleBasedEntityExtractor()
    assert extractor.name is NlpMethod.RULE_BASED
    metadata = extractor.metadata()
    assert metadata["requires_download"] is False
    assert "not semantic" in metadata["description"]


def test_entity_records_carry_provenance_and_normalised_text() -> None:
    records = entity_records(
        RuleBasedEntityExtractor(),
        TEXT,
        course_id="demo-course",
        owner_type=TextOwnerType.DOCUMENT,
        owner_id="doc_1",
        document_id="doc_1",
    )
    assert records
    record = records[0]
    assert record.course_id == "demo-course"
    assert record.owner_type is TextOwnerType.DOCUMENT
    assert record.owner_id == "doc_1"
    assert record.document_id == "doc_1"
    assert record.normalized == normalize_entity(record.text)
    assert record.method is NlpMethod.RULE_BASED


def test_spacy_provider_reports_availability_honestly() -> None:
    extractor = SpacyEntityExtractor()
    assert isinstance(extractor.installed(), bool)
    assert isinstance(extractor.available(), bool)
    if not extractor.installed():
        assert extractor.available() is False


def test_build_extractor_falls_back_when_spacy_is_missing() -> None:
    extractor, warnings = build_extractor("spacy")
    if SpacyEntityExtractor().available():  # pragma: no cover - depends on environment
        assert extractor.name is NlpMethod.SPACY
        assert warnings == []
    else:
        assert extractor.name is NlpMethod.RULE_BASED
        assert [warning.code for warning in warnings] == ["model_unavailable"]


class StubExtractor:
    """Proves the protocol is replaceable without touching the service."""

    name = NlpMethod.RULE_BASED

    def extract(self, text: str) -> list[RawEntity]:
        return [RawEntity(text="stub concept", label="STUB", start=0, end=12, confidence=0.42)]

    def metadata(self) -> dict[str, str]:
        return {"provider": "stub"}


def test_custom_extractor_protocol_is_honoured() -> None:
    records = entity_records(
        StubExtractor(),
        "stub concept appears here",
        course_id="c",
        owner_type=TextOwnerType.SECTION,
        owner_id="sec_1",
    )
    assert len(records) == 1
    assert records[0].label == "STUB"
    assert records[0].confidence == 0.42
    assert records[0].start_char == 0
