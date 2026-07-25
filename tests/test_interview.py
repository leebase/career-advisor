"""Interview engine: JSON validation, fact upsert, resumable turns (fake LLM)."""

import json

import pytest

from career_advisor import db, interview
from career_advisor.profile import load_schema


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREER_ADVISOR_DB", str(tmp_path / "test.db"))
    c = db.connect()
    yield c
    c.close()


def _session(conn, name="Tester"):
    code = db.create_invite(conn, name)
    token = db.create_session(conn, code)
    return token, code


def _scripted_complete(responses: list[str]):
    """Return a complete_fn that walks a fixed list of model outputs."""
    queue = list(responses)

    def complete(prompt: str, **kwargs) -> str:
        if not queue:
            raise AssertionError(
                f"Unexpected extra LLM call. Prompt head: {prompt[:200]}"
            )
        return queue.pop(0)

    return complete


def test_validate_extraction_rejects_bad_keys_and_keeps_good():
    schema = load_schema("candidate")
    data = {
        "extracted_facts": [
            {
                "key": "identity.full_name",
                "value": "Lee",
                "evidence": "said so",
                "confidence": 0.9,
            },
            {"key": "not.a.real.slot", "value": "x", "evidence": "y"},
            {"key": "identity.location", "value": "", "evidence": ""},
        ],
        "follow_up_question": "What systems did you own?",
        "section": "employment_history",
        "rationale": "dig",
        "interview_complete": False,
    }
    out = interview._validate_extraction(data, schema)
    assert len(out["extracted_facts"]) == 1
    assert out["extracted_facts"][0]["key"] == "identity.full_name"
    assert out["follow_up_question"].startswith("What systems")


def test_validate_requires_question_unless_complete():
    schema = load_schema("candidate")
    with pytest.raises(ValueError):
        interview._validate_extraction(
            {
                "extracted_facts": [],
                "follow_up_question": None,
                "interview_complete": False,
            },
            schema,
        )


def test_parse_json_from_fence():
    data = interview._extract_json_object(
        'Here you go:\n```json\n{"a": 1}\n```\n'
    )
    assert data == {"a": 1}


def test_opening_and_submit_resume(conn):
    token, code = _session(conn)
    opening = json.dumps(
        {
            "extracted_facts": [],
            "follow_up_question": "What's your name and target role?",
            "section": "identity",
            "rationale": "start",
            "interview_complete": False,
        }
    )
    after = json.dumps(
        {
            "extracted_facts": [
                {
                    "key": "identity.full_name",
                    "value": "Young Man",
                    "evidence": "self-identified",
                    "confidence": 0.95,
                },
                {
                    "key": "career_summary.target_roles",
                    "value": "Desktop Support / SysAdmin",
                    "evidence": "stated goal",
                    "confidence": 0.9,
                },
            ],
            "follow_up_question": "How many seats did you support?",
            "section": "employment_history",
            "rationale": "dig scale",
            "interview_complete": False,
        }
    )
    complete_fn = _scripted_complete([opening, after])

    r1 = interview.ensure_opening_question(
        conn, token, code, complete_fn=complete_fn
    )
    assert r1.question == "What's your name and target role?"
    assert r1.interview_complete is False

    # Resume without new LLM call — open turn still unanswered.
    r1b = interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([])
    )
    assert r1b.question == r1.question

    r2 = interview.submit_answer(
        conn,
        token,
        code,
        "I'm Young Man, looking for Desktop Support or SysAdmin roles.",
        complete_fn=complete_fn,
    )
    assert "seats" in (r2.question or "")
    facts = db.list_profile_facts(conn, token)
    keys = {f["fact_key"] for f in facts}
    assert "identity.full_name" in keys
    assert "career_summary.target_roles" in keys


def test_submit_empty_answer_keeps_question(conn):
    token, code = _session(conn)
    opening = json.dumps(
        {
            "extracted_facts": [],
            "follow_up_question": "First question?",
            "section": "identity",
            "rationale": "x",
            "interview_complete": False,
        }
    )
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([opening])
    )
    result = interview.submit_answer(
        conn, token, code, "   ", complete_fn=_scripted_complete([])
    )
    assert result.error
    assert result.question == "First question?"


def test_interview_complete_path(conn):
    token, code = _session(conn)
    opening = json.dumps(
        {
            "extracted_facts": [],
            "follow_up_question": "Anything else?",
            "section": "preferences",
            "rationale": "wrap",
            "interview_complete": False,
        }
    )
    done = json.dumps(
        {
            "extracted_facts": [
                {
                    "key": "preferences.work_arrangement",
                    "value": "Remote preferred",
                    "evidence": "stated",
                    "confidence": 0.8,
                }
            ],
            "follow_up_question": None,
            "section": "preferences",
            "rationale": "done",
            "interview_complete": True,
        }
    )
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([opening])
    )
    result = interview.submit_answer(
        conn, token, code, "I want remote work.", complete_fn=_scripted_complete([done])
    )
    assert result.interview_complete is True
    assert result.question is None
    assert db.interview_complete_flag(conn, token) is True


def _turn_json(question, target=None, facts=None, complete=False, rationale="x"):
    return json.dumps(
        {
            "extracted_facts": facts or [],
            "follow_up_question": question,
            "section": "employment_history",
            "target_fact_key": target,
            "rationale": rationale,
            "interview_complete": complete,
        }
    )


def _seed_answered_turns(conn, token, code, count, target=None):
    """Insert ``count`` already-answered turns straight into the DB."""
    for i in range(count):
        idx = db.next_turn_index(conn, token)
        row_id = db.insert_interview_turn(
            conn,
            session_token=token,
            invite_code=code,
            turn_index=idx,
            question=f"Seeded question {i}?",
            section="employment_history",
            target_key=target,
        )
        db.complete_interview_turn(conn, row_id, f"answer {i}", None)


# --- Fix 1: the model can see what it already asked ---------------------------


def test_prompt_shows_previously_asked_questions(conn):
    """Regression: the prompt carried no question history, so the engine
    re-asked "what did you personally own day to day" 15 times in one
    interview and told the user it could not see earlier answers."""
    token, code = _session(conn)
    _seed_answered_turns(conn, token, code, 2)

    seen = {}

    def capture(prompt: str, **kwargs) -> str:
        seen["prompt"] = prompt
        return _turn_json("A brand new question?", "employment_history.environments")

    interview.ensure_opening_question(conn, token, code, complete_fn=capture)

    assert "Seeded question 0?" in seen["prompt"]
    assert "Seeded question 1?" in seen["prompt"]
    assert "QUESTIONS ALREADY ASKED" in seen["prompt"]
    assert "NEVER REPEAT YOURSELF" in seen["prompt"]


def test_submit_answer_passes_question_history(conn):
    token, code = _session(conn)
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([_turn_json("First?")])
    )
    seen = {}

    def capture(prompt: str, **kwargs) -> str:
        seen["prompt"] = prompt
        return _turn_json("Second?", "employment_history.environments")

    interview.submit_answer(conn, token, code, "an answer", complete_fn=capture)
    assert "First?" in seen["prompt"]


# --- Fix 2: per-slot dig cap --------------------------------------------------


def test_slot_is_settled_after_the_dig_cap(conn):
    """Two slots consumed the last third of the first real interview because
    nothing capped how often one slot could be dug at."""
    token, code = _session(conn)
    key = "employment_history.scope_of_responsibility"
    _seed_answered_turns(
        conn, token, code, interview.MAX_DIGS_PER_SLOT, target=key
    )

    assert key in interview._exhausted_keys(conn, token)
    _schema, _facts, cov = interview._load_state(conn, token)
    assert key not in cov.open_gaps

    seen = {}

    def capture(prompt: str, **kwargs) -> str:
        seen["prompt"] = prompt
        return _turn_json("Something else?", "education.formal_education")

    interview.ensure_opening_question(conn, token, code, complete_fn=capture)
    settled_block = seen["prompt"].split("SETTLED")[1]
    assert key in settled_block


def test_target_key_is_recorded_on_the_turn(conn):
    token, code = _session(conn)
    interview.ensure_opening_question(
        conn,
        token,
        code,
        complete_fn=_scripted_complete(
            [_turn_json("Scope?", "employment_history.scope_of_responsibility")]
        ),
    )
    turns = db.list_interview_turns(conn, token)
    assert turns[-1]["target_key"] == "employment_history.scope_of_responsibility"


def test_unknown_target_key_is_dropped_not_fatal(conn):
    token, code = _session(conn)
    result = interview.ensure_opening_question(
        conn,
        token,
        code,
        complete_fn=_scripted_complete([_turn_json("Q?", "not.a.real.slot")]),
    )
    assert result.question == "Q?"
    assert db.list_interview_turns(conn, token)[-1]["target_key"] is None


# --- Fix 3: the candidate sees what their answer bought -----------------------


def test_submit_answer_returns_captured_facts_and_rationale(conn):
    token, code = _session(conn)
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([_turn_json("Name?")])
    )
    extracted = [
        {
            "key": "identity.full_name",
            "value": "Alex Rivera",
            "evidence": "introduced themselves",
            "confidence": 0.95,
        }
    ]
    result = interview.submit_answer(
        conn,
        token,
        code,
        "I'm Alex Rivera.",
        complete_fn=_scripted_complete(
            [
                _turn_json(
                    "Where are you based?",
                    "identity.location",
                    facts=extracted,
                    rationale="Location drives which roles are realistic.",
                )
            ]
        ),
    )
    assert len(result.captured) == 1
    captured = result.captured[0]
    assert captured["value"] == "Alex Rivera"
    assert captured["label"]  # human-readable slot description, not the key
    assert captured["updated"] is False
    assert result.rationale == "Location drives which roles are realistic."


# --- Fix 4: real endings ------------------------------------------------------


def test_candidate_can_finish_the_interview(conn):
    """"Have I given you enough information yet?" had no code path. Now it
    does."""
    token, code = _session(conn)
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([_turn_json("Q?")])
    )
    result = interview.finish_now(conn, token)
    assert result.interview_complete is True
    assert db.interview_complete_flag(conn, token) is True

    # And the engine does not resurrect a question afterwards.
    again = interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([])
    )
    assert again.interview_complete is True
    assert again.question is None


def test_hard_turn_cap_ends_the_interview(conn):
    token, code = _session(conn)
    _seed_answered_turns(conn, token, code, interview.HARD_TURN_CAP - 1)
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([_turn_json("Last?")])
    )
    result = interview.submit_answer(
        conn,
        token,
        code,
        "final answer",
        complete_fn=_scripted_complete([_turn_json("Another one?")]),
    )
    assert result.interview_complete is True
    assert result.question is None
    row = db.interview_finished(conn, token)
    assert row is not None and row["finished_by"] == "turn_cap"


def test_progress_context_reports_nudge_and_readiness(conn):
    token, code = _session(conn)
    _seed_answered_turns(conn, token, code, interview.SOFT_TURN_NUDGE)
    progress = interview.progress_context(conn, token)
    assert progress["nudge_finish"] is True
    assert progress["ready"] is False
    assert progress["turns_remaining"] == (
        interview.HARD_TURN_CAP - interview.SOFT_TURN_NUDGE
    )


# --- Fix 5: a tired answer cannot clobber a better one ------------------------


def test_weaker_answer_does_not_overwrite_a_stronger_fact(conn):
    """The first user's stored scope_of_responsibility ended up as a throwaway
    late answer that had overwritten far richer earlier content."""
    token, code = _session(conn)
    key = "employment_history.scope_of_responsibility"
    db.upsert_profile_fact(
        conn,
        session_token=token,
        invite_code=code,
        fact_key=key,
        value="Owned the queue for 800 users across 15 sites",
        evidence="sole escalation point",
        confidence=0.9,
    )
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([_turn_json("Q?")])
    )
    weak = [
        {
            "key": key,
            "value": "Kept things tidy",
            "evidence": "habitual practice",
            "confidence": 0.8,
        }
    ]
    result = interview.submit_answer(
        conn,
        token,
        code,
        "I keep things tidy.",
        complete_fn=_scripted_complete([_turn_json("Next?", facts=weak)]),
    )

    stored = db.get_profile_fact(conn, token, key)
    assert "800 users" in stored["value"]
    assert result.captured == []
    assert result.kept_earlier and result.kept_earlier[0]["key"] == key

    # Rejected values stay recoverable rather than vanishing.
    history = db.list_fact_history(conn, token, key)
    assert [h["accepted"] for h in history] == [0]
    assert history[0]["value"] == "Kept things tidy"


def test_stronger_answer_does_overwrite(conn):
    token, code = _session(conn)
    key = "employment_history.scope_of_responsibility"
    db.upsert_profile_fact(
        conn,
        session_token=token,
        invite_code=code,
        fact_key=key,
        value="Ran the help desk",
        evidence="",
        confidence=0.9,
    )
    interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([_turn_json("Q?")])
    )
    better = [
        {
            "key": key,
            "value": "Ran a help desk of 6 techs for 800 users",
            "evidence": "final escalation owner",
            "confidence": 0.85,
        }
    ]
    result = interview.submit_answer(
        conn,
        token,
        code,
        "Six techs, 800 users.",
        complete_fn=_scripted_complete([_turn_json("Next?", facts=better)]),
    )
    assert "6 techs" in db.get_profile_fact(conn, token, key)["value"]
    assert result.captured[0]["updated"] is True


def test_retry_on_bad_json_then_success(conn):
    token, code = _session(conn)
    bad = "not json at all"
    good = json.dumps(
        {
            "extracted_facts": [],
            "follow_up_question": "Recovered question?",
            "section": "identity",
            "rationale": "retry",
            "interview_complete": False,
        }
    )
    result = interview.ensure_opening_question(
        conn, token, code, complete_fn=_scripted_complete([bad, good])
    )
    assert result.question == "Recovered question?"
