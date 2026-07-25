"""Smoke tests for the Career Advisor web shell: invite → session → home."""

import pytest
from fastapi.testclient import TestClient

from career_advisor import db
from career_advisor.web import app, limiter


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREER_ADVISOR_DB", str(tmp_path / "test.db"))
    limiter._failures.clear()
    # https base: the session cookie is Secure (public access is HTTPS).
    return TestClient(app, base_url="https://testserver")


def make_invite(name="Test User"):
    conn = db.connect()
    try:
        return db.create_invite(conn, name)
    finally:
        conn.close()


def test_health(client):
    resp = client.get("/career-advisor/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_root_redirects_to_app(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/career-advisor/"


def test_no_session_shows_welcome(client):
    resp = client.get("/career-advisor/")
    assert resp.status_code == 200
    assert "invitation" in resp.text.lower()


def test_invalid_invite_rejected(client):
    resp = client.get("/career-advisor/", params={"invite": "bogus"})
    assert resp.status_code == 403
    assert "not valid" in resp.text


def test_valid_invite_creates_session_and_clean_url(client):
    code = make_invite("Young Man")
    resp = client.get(
        "/career-advisor/", params={"invite": code}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/career-advisor/"
    assert "ca_session" in resp.cookies

    home = client.get("/career-advisor/")
    assert home.status_code == 200
    assert "Young Man" in home.text


def test_revoked_invite_kills_session(client):
    code = make_invite("Revoked User")
    client.get("/career-advisor/", params={"invite": code})
    conn = db.connect()
    try:
        db.revoke_invite(conn, code)
    finally:
        conn.close()
    resp = client.get("/career-advisor/")
    assert "Revoked User" not in resp.text


def test_invite_brute_force_rate_limited(client):
    for _ in range(10):
        client.get("/career-advisor/", params={"invite": "guess"})
    resp = client.get("/career-advisor/", params={"invite": "guess"})
    assert resp.status_code == 429
