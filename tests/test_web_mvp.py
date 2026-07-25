"""Web routes for interview + documents with injected fake LLM via monkeypatch."""

import json

import pytest
from fastapi.testclient import TestClient

from career_advisor import db
from career_advisor.web import app, limiter


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREER_ADVISOR_DB", str(tmp_path / "test.db"))
    limiter._failures.clear()
    return TestClient(app, base_url="https://testserver")


def login(client, name="Young Man"):
    conn = db.connect()
    try:
        code = db.create_invite(conn, name)
    finally:
        conn.close()
    resp = client.get(
        "/career-advisor/", params={"invite": code}, follow_redirects=False
    )
    assert resp.status_code == 303
    return code


def test_home_cards_link_to_features(client):
    login(client)
    home = client.get("/career-advisor/")
    assert home.status_code == 200
    assert "Coming soon" not in home.text
    assert 'href="/career-advisor/interview"' in home.text
    assert 'href="/career-advisor/documents"' in home.text


def test_interview_requires_session(client):
    resp = client.get("/career-advisor/interview", follow_redirects=False)
    assert resp.status_code == 303


def test_interview_flow_with_fake_llm(client, monkeypatch):
    login(client)

    responses = [
        json.dumps(
            {
                "extracted_facts": [],
                "follow_up_question": "What is your full name?",
                "section": "identity",
                "rationale": "start",
                "interview_complete": False,
            }
        ),
        json.dumps(
            {
                "extracted_facts": [
                    {
                        "key": "identity.full_name",
                        "value": "Young Man",
                        "evidence": "answered",
                        "confidence": 0.9,
                    }
                ],
                "follow_up_question": "What role do you want next?",
                "section": "career_summary",
                "rationale": "next",
                "interview_complete": False,
            }
        ),
    ]

    def fake_complete(prompt: str, **kwargs) -> str:
        if not responses:
            raise AssertionError("out of scripted responses")
        return responses.pop(0)

    monkeypatch.setattr("career_advisor.interview.llm.complete", fake_complete)

    page = client.get("/career-advisor/interview")
    assert page.status_code == 200
    assert "What is your full name?" in page.text
    assert "Section progress" in page.text or "Identity" in page.text

    page2 = client.post(
        "/career-advisor/interview",
        data={"answer": "Young Man"},
    )
    assert page2.status_code == 200
    assert "What role do you want next?" in page2.text


def test_answer_page_shows_what_was_captured_and_why(client, monkeypatch):
    """The engine always knew what an answer produced and why it was asking
    next; the page showed neither, which is what made it feel like a void."""
    login(client)

    responses = [
        json.dumps(
            {
                "extracted_facts": [],
                "follow_up_question": "What is your full name?",
                "section": "identity",
                "target_fact_key": "identity.full_name",
                "rationale": "start",
                "interview_complete": False,
            }
        ),
        json.dumps(
            {
                "extracted_facts": [
                    {
                        "key": "identity.full_name",
                        "value": "Young Man",
                        "evidence": "answered directly",
                        "confidence": 0.95,
                    }
                ],
                "follow_up_question": "Where are you based?",
                "section": "identity",
                "target_fact_key": "identity.location",
                "rationale": "Location decides which roles are realistic.",
                "interview_complete": False,
            }
        ),
    ]

    def fake_complete(prompt: str, **kwargs) -> str:
        return responses.pop(0)

    monkeypatch.setattr("career_advisor.interview.llm.complete", fake_complete)

    client.get("/career-advisor/interview")
    page = client.post("/career-advisor/interview", data={"answer": "Young Man"})

    assert "Captured from that answer" in page.text
    assert "Young Man" in page.text
    assert "Location decides which roles are realistic." in page.text
    # And an exit is always on screen.
    assert "finish up" in page.text
    assert 'action="/career-advisor/interview/finish"' in page.text


def test_finish_button_ends_the_interview(client, monkeypatch):
    login(client)
    monkeypatch.setattr(
        "career_advisor.interview.llm.complete",
        lambda prompt, **kwargs: json.dumps(
            {
                "extracted_facts": [],
                "follow_up_question": "Another question?",
                "section": "identity",
                "rationale": "x",
                "interview_complete": False,
            }
        ),
    )

    client.get("/career-advisor/interview")
    resp = client.post("/career-advisor/interview/finish", follow_redirects=False)
    assert resp.status_code == 303

    page = client.get("/career-advisor/interview")
    assert "Interview complete" in page.text
    assert "Another question?" not in page.text


def test_documents_generate_and_download(client, monkeypatch):
    login(client, "Lee")

    # Seed profile facts for the session.
    conn = db.connect()
    try:
        # Find the session token from the cookie the client holds.
        token = client.cookies.get("ca_session")
        assert token
        invite = conn.execute(
            "SELECT invite_code FROM sessions WHERE token = ?", (token,)
        ).fetchone()["invite_code"]
        db.upsert_profile_fact(
            conn,
            session_token=token,
            invite_code=invite,
            fact_key="identity.full_name",
            value="Lee",
            evidence="self",
            confidence=0.99,
        )
        db.upsert_profile_fact(
            conn,
            session_token=token,
            invite_code=invite,
            fact_key="skills_and_stack.core_skills",
            value="Windows, Active Directory, AWS",
            evidence="daily admin work",
            confidence=0.9,
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        "career_advisor.documents.llm.complete",
        lambda prompt, **kw: "# Lee\n\n## Summary\nStrong sysadmin.\n",
    )

    list_page = client.get("/career-advisor/documents")
    assert list_page.status_code == 200
    assert "Generate resume" in list_page.text

    gen = client.post(
        "/career-advisor/documents/generate",
        data={"doc_type": "resume"},
        follow_redirects=False,
    )
    assert gen.status_code == 303
    loc = gen.headers["location"]
    assert loc.startswith("/career-advisor/documents/")

    view = client.get(loc)
    assert view.status_code == 200
    assert "Strong sysadmin" in view.text
    assert "Download Markdown" in view.text

    doc_id = int(loc.rstrip("/").split("/")[-1])
    dl = client.get(f"/career-advisor/documents/{doc_id}/download")
    assert dl.status_code == 200
    assert "Strong sysadmin" in dl.text
    assert "attachment" in dl.headers.get("content-disposition", "")

    # Second generate → version 2 still keeps v1.
    monkeypatch.setattr(
        "career_advisor.documents.llm.complete",
        lambda prompt, **kw: "# Lee\n\n## Summary\nVersion two.\n",
    )
    gen2 = client.post(
        "/career-advisor/documents/generate",
        data={"doc_type": "resume"},
        follow_redirects=True,
    )
    assert gen2.status_code == 200
    assert "Version two" in gen2.text

    docs_page = client.get("/career-advisor/documents")
    assert "v1" in docs_page.text and "v2" in docs_page.text
