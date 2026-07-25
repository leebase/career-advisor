"""Interview session review — repeat detection, engagement, pushback."""

import pytest

from career_advisor import db, review


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREER_ADVISOR_DB", str(tmp_path / "test.db"))
    c = db.connect()
    yield c
    c.close()


def _seed(conn, pairs, name="Tester"):
    """pairs: list of (question, answer|None). Returns the session token."""
    code = db.create_invite(conn, name)
    token = db.create_session(conn, code)
    for i, (question, answer) in enumerate(pairs):
        row_id = db.insert_interview_turn(
            conn,
            session_token=token,
            invite_code=code,
            turn_index=i,
            question=question,
            section="employment_history",
        )
        if answer is not None:
            db.complete_interview_turn(conn, row_id, answer, None)
    return token, code


def test_detects_reworded_repeat_questions(conn):
    """The complaint was "the same questions multiple times" — reworded
    duplicates have to count, not just literal ones."""
    token, _ = _seed(
        conn,
        [
            ("What did you personally own day to day at Northwind?", "Servers."),
            ("Which systems did you personally own day to day at Northwind?", "Same."),
            ("What certifications do you hold?", "A+"),
        ],
    )
    result = review.review_session(conn, token)
    assert len(result.repeats) == 1
    pair = result.repeats[0]
    assert (pair.first_index, pair.second_index) == (0, 1)
    assert pair.score >= review.REPEAT_SIMILARITY


def test_distinct_questions_are_not_flagged(conn):
    token, _ = _seed(
        conn,
        [
            ("What certifications do you hold?", "A+"),
            ("Where are you based, and will you relocate?", "Chicago."),
            ("What salary range works for you?", "90k"),
        ],
    )
    assert review.review_session(conn, token).repeats == []


def test_flags_candidate_pushback(conn):
    """A candidate saying "I already answered that" is a product failure the
    review must surface, not a line buried in the transcript."""
    token, _ = _seed(
        conn,
        [
            ("What did you own?", "I have already answered that twice"),
            ("And your scope?", "Have I given you enough information yet?"),
            ("Certifications?", "A+ and Network+"),
        ],
    )
    result = review.review_session(conn, token)
    assert [i for i, _ in result.frustration] == [0, 1]


def test_reports_engagement_decay_and_yield(conn):
    token, code = _seed(
        conn,
        [("Question %d?" % i, "x" * 400) for i in range(10)]
        + [("Late question %d?" % i, "no") for i in range(10)],
    )
    db.upsert_profile_fact(
        conn,
        session_token=token,
        invite_code=code,
        fact_key="identity.full_name",
        value="Tester",
        evidence="stated",
        confidence=0.9,
    )
    result = review.review_session(conn, token)
    assert result.turn_count == 20
    assert result.first_answers_avg == 400
    assert result.last_answers_avg == 2
    assert result.turns_per_fact == 20.0
    assert "answers shrank" in review.format_review(result)


def test_counts_unanswered_turns_and_finish_reason(conn):
    token, _ = _seed(conn, [("Q1?", "a"), ("Q2?", None)])
    result = review.review_session(conn, token)
    assert (result.answered, result.unanswered) == (1, 1)
    assert result.finished_by is None

    db.finish_interview(conn, token, finished_by="candidate")
    assert review.review_session(conn, token).finished_by == "candidate"


def test_list_sessions_orders_by_turn_count(conn):
    small, _ = _seed(conn, [("Q?", "a")], name="Small")
    big, _ = _seed(conn, [("Q%d?" % i, "a") for i in range(5)], name="Big")
    rows = review.list_sessions(conn)
    assert [r["session_token"] for r in rows] == [big, small]
    assert rows[0]["turns"] == 5
