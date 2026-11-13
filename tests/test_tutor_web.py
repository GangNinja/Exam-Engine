from __future__ import annotations

from exam_engine.config import TutorSettings
from exam_engine.dashboard.tutor import _compose_web_answer
from exam_engine.dashboard.websearch import WebSource


def test_fast_web_answer_labels_cited_evidence() -> None:
    source = WebSource(
        source_id="W1",
        title="Open-channel hydraulics",
        url="https://example.test/hydraulics",
        snippet=(
            "Specific energy is the total head measured above the channel bed. "
            "Critical flow occurs when the Froude number equals one. "
            "A hydraulic jump is a sudden transition from supercritical to subcritical flow."
        ),
        provider="wikipedia",
    )

    answer = _compose_web_answer(
        "Explain specific energy, critical flow and hydraulic jump",
        None,
        [source],
    )

    assert answer is not None
    assert "Specific energy:" in answer.answer
    assert "Critical flow:" in answer.answer
    assert "Hydraulic jump:" in answer.answer
    assert "[W1]" in answer.answer
    assert answer.citation_ids == ["W1"]


def test_fast_web_answer_rejects_off_topic_sources() -> None:
    source = WebSource(
        source_id="W1",
        title="Unrelated page",
        url="https://example.test/unrelated",
        snippet="A compiler translates source code into machine instructions.",
        provider="wikipedia",
    )

    assert _compose_web_answer("Explain channel hydraulics", None, [source]) is None


def test_tutor_defaults_to_fast_web_first_with_a_ten_second_cap() -> None:
    settings = TutorSettings()
    assert settings.timeout_seconds == 10.0
    assert settings.web_first is True
    assert settings.max_web_sources == 6
