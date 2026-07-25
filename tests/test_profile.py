"""Domain schema loading and coverage classification."""

from pathlib import Path

from career_advisor.profile import (
    classify_fact,
    coverage,
    is_degradation,
    load_schema,
    schema_as_prompt_block,
)


def test_load_candidate_schema():
    schema = load_schema("candidate")
    assert schema.domain == "candidate"
    assert schema.version == 1
    assert len(schema.slots) >= 10
    keys = schema.slot_map()
    assert "identity.full_name" in keys
    assert "achievements.key_wins" in keys
    assert keys["identity.full_name"].evidence
    assert keys["identity.full_name"].priority == "required"


def test_schema_prompt_includes_evidence():
    schema = load_schema("candidate")
    block = schema_as_prompt_block(schema)
    assert "identity.full_name" in block
    assert "evidence needed" in block


def test_coverage_empty_weak_filled():
    schema = load_schema("candidate")
    facts = [
        {
            "fact_key": "identity.full_name",
            "value": "Alex Rivera",
            "evidence": "self-reported preferred name",
            "confidence": 0.9,
        },
        {
            "fact_key": "achievements.key_wins",
            "value": "improved things",
            "evidence": "",
            "confidence": 0.5,
        },
    ]
    cov = coverage(schema, facts)
    assert "identity.full_name" in cov.filled
    assert "achievements.key_wins" in cov.weak
    assert "preferences.work_arrangement" in cov.empty
    assert "achievements.key_wins" in cov.open_gaps


def test_classify_fact_metrics():
    schema = load_schema("candidate")
    slot = schema.slot_map()["achievements.key_wins"]
    assert (
        classify_fact(
            slot,
            "Cut ticket backlog 40% in 3 months",
            "ServiceNow queue ownership",
            0.85,
        )
        == "filled"
    )
    assert classify_fact(slot, "helped people sometimes", "", 0.5) == "weak"
    assert classify_fact(slot, "", "", None) == "empty"


def test_confident_evidenced_fact_is_filled_without_a_number():
    """Regression: the two slots that ate a third of the first real interview.

    Both were stored at high confidence with real evidence text, but neither
    contained a digit, so the metric heuristic kept them "weak" — which kept
    them in open_gaps, which kept the engine digging at them forever.
    """
    schema = load_schema("candidate")
    slot = schema.slot_map()["skills_and_stack.core_skills"]
    verdict = classify_fact(
        slot,
        "Extensive hands-on use of all three core systems across every role.",
        "Candidate stated they used all three extensively at all jobs.",
        0.78,
    )
    assert verdict == "filled"


def test_low_confidence_still_weak_even_with_evidence():
    schema = load_schema("candidate")
    slot = schema.slot_map()["skills_and_stack.core_skills"]
    assert classify_fact(slot, "some tools", "vague mention", 0.3) == "weak"


def test_exhausted_weak_slot_is_accepted_and_leaves_open_gaps():
    schema = load_schema("candidate")
    facts = [
        {
            "fact_key": "achievements.key_wins",
            "value": "improved things",
            "evidence": "",
            "confidence": 0.5,
        }
    ]
    cov = coverage(schema, facts)
    assert "achievements.key_wins" in cov.open_gaps

    capped = coverage(
        schema, facts, exhausted_keys={"achievements.key_wins"}
    )
    assert "achievements.key_wins" in capped.accepted
    assert "achievements.key_wins" not in capped.open_gaps


def test_exhausted_empty_slot_is_skipped_not_re_asked():
    schema = load_schema("candidate")
    cov = coverage(schema, [], exhausted_keys={"preferences.compensation"})
    assert "preferences.compensation" in cov.skipped
    assert "preferences.compensation" not in cov.open_gaps


def test_section_progress_counts_accepted_slots():
    schema = load_schema("candidate")
    facts = [
        {
            "fact_key": "achievements.key_wins",
            "value": "led the migration",
            "evidence": "",
            "confidence": 0.5,
        }
    ]
    before = {
        r["section_id"]: r
        for r in coverage(schema, facts).section_progress(schema)
    }
    after = {
        r["section_id"]: r
        for r in coverage(
            schema, facts, exhausted_keys={"achievements.key_wins"}
        ).section_progress(schema)
    }
    assert before["achievements"]["filled"] == 0
    assert after["achievements"]["filled"] == 1


def test_ready_for_documents_tracks_required_slots():
    schema = load_schema("candidate")
    assert coverage(schema, []).ready_for_documents(schema) is False
    required = [s.key for s in schema.slots if s.priority == "required"]
    facts = [
        {
            "fact_key": key,
            "value": "answered with 12 sites and 400 users",
            "evidence": "stated in interview",
            "confidence": 0.9,
        }
        for key in required
    ]
    assert coverage(schema, facts).ready_for_documents(schema) is True


def test_degradation_blocks_losing_the_numbers():
    existing = {
        "value": "Owned 800 endpoints across 15 sites",
        "evidence": "ticket queue ownership",
        "confidence": 0.9,
    }
    incoming = {
        "value": "Handled a lot of devices",
        "evidence": "",
        "confidence": 0.8,
    }
    blocked, reason = is_degradation(existing, incoming)
    assert blocked is True
    assert reason


def test_degradation_allows_improvement_and_corrections():
    existing = {"value": "Handled devices", "evidence": "", "confidence": 0.9}
    better = {
        "value": "Owned 800 endpoints across 15 sites",
        "evidence": "queue ownership",
        "confidence": 0.7,
    }
    assert is_degradation(existing, better)[0] is False

    # An explicit correction wins even when it drops detail.
    rich = {"value": "Supported 900 users", "evidence": "x", "confidence": 0.9}
    correction = {
        "value": "Actually it was a different team",
        "evidence": "",
        "confidence": 0.6,
        "status": "contradicted",
    }
    assert is_degradation(rich, correction)[0] is False


def test_degradation_allows_first_write():
    assert is_degradation(None, {"value": "anything"})[0] is False


def test_load_generic_schema_shape(tmp_path: Path):
    path = tmp_path / "widget.yaml"
    path.write_text(
        """
domain: widget
version: 2
title: Widget Profile
sections:
  basics:
    title: Basics
    order: 1
    facts:
      name:
        description: Widget name
        evidence: Label on the box
        priority: required
""",
        encoding="utf-8",
    )
    schema = load_schema("widget", path=path)
    assert schema.domain == "widget"
    assert schema.version == 2
    assert schema.slots[0].key == "basics.name"
