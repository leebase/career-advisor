"""Document generation and versioning (fake LLM)."""

import pytest

from career_advisor import db, documents


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREER_ADVISOR_DB", str(tmp_path / "test.db"))
    c = db.connect()
    yield c
    c.close()


def _session_with_facts(conn):
    code = db.create_invite(conn, "Lee")
    token = db.create_session(conn, code)
    db.upsert_profile_fact(
        conn,
        session_token=token,
        invite_code=code,
        fact_key="identity.full_name",
        value="Lee Harrington",
        evidence="self",
        confidence=0.99,
    )
    db.upsert_profile_fact(
        conn,
        session_token=token,
        invite_code=code,
        fact_key="career_summary.target_roles",
        value="Systems Administrator",
        evidence="goal",
        confidence=0.9,
    )
    db.upsert_profile_fact(
        conn,
        session_token=token,
        invite_code=code,
        fact_key="achievements.key_wins",
        value="Reduced ticket backlog 30% over 6 months",
        evidence="ServiceNow metrics as queue owner",
        confidence=0.85,
    )
    return token, code


def test_generate_requires_facts(conn):
    code = db.create_invite(conn, "Empty")
    token = db.create_session(conn, code)
    row, err = documents.generate_document(
        conn,
        session_token=token,
        invite_code=code,
        doc_type=documents.DOC_RESUME,
        candidate_name="Empty",
        complete_fn=lambda p: "# Resume",
    )
    assert row is None
    assert err and "interview" in err.lower()


def test_generate_resume_and_strategy_versions(conn):
    token, code = _session_with_facts(conn)
    bodies = {"n": 0}

    def complete(prompt: str, **kwargs) -> str:
        bodies["n"] += 1
        if "Resume Architect" in prompt:
            return f"# Lee Harrington\n\n## Summary\nSysAdmin v{bodies['n']}\n"
        return f"# Strategy\nWeekly plan v{bodies['n']}\n"

    r1, err = documents.generate_document(
        conn,
        session_token=token,
        invite_code=code,
        doc_type=documents.DOC_RESUME,
        candidate_name="Lee",
        complete_fn=complete,
    )
    assert err is None
    assert r1["version"] == 1
    assert "SysAdmin" in r1["body_markdown"]

    r2, err = documents.generate_document(
        conn,
        session_token=token,
        invite_code=code,
        doc_type=documents.DOC_RESUME,
        candidate_name="Lee",
        complete_fn=complete,
    )
    assert err is None
    assert r2["version"] == 2
    assert r1["id"] != r2["id"]

    s1, err = documents.generate_document(
        conn,
        session_token=token,
        invite_code=code,
        doc_type=documents.DOC_STRATEGY,
        candidate_name="Lee",
        complete_fn=complete,
    )
    assert err is None
    assert s1["version"] == 1
    assert "Weekly plan" in s1["body_markdown"]

    versions = db.list_documents_by_type(conn, token, documents.DOC_RESUME)
    assert [v["version"] for v in versions] == [2, 1]


def test_unknown_doc_type(conn):
    token, code = _session_with_facts(conn)
    row, err = documents.generate_document(
        conn,
        session_token=token,
        invite_code=code,
        doc_type="cover_letter",
        candidate_name="Lee",
        complete_fn=lambda p: "x",
    )
    assert row is None
    assert "Unknown" in (err or "")
