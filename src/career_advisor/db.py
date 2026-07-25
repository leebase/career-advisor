"""SQLite persistence for Career Advisor.

Founder rule: no secrets in environment variables. Invite codes and session
tokens live only in the database file; CAREER_ADVISOR_DB holds a path, not a
credential.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "data/career-advisor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    invite_code TEXT NOT NULL REFERENCES invites(code),
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL REFERENCES sessions(token),
    invite_code TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT 'candidate',
    fact_key TEXT NOT NULL,
    value TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL,
    source_turn INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_token, domain, fact_key)
);
CREATE TABLE IF NOT EXISTS interview_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL REFERENCES sessions(token),
    invite_code TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT 'candidate',
    turn_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    extraction_json TEXT,
    section TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(session_token, domain, turn_index)
);
CREATE TABLE IF NOT EXISTS interview_state (
    session_token TEXT NOT NULL REFERENCES sessions(token),
    domain TEXT NOT NULL DEFAULT 'candidate',
    finished_at TEXT NOT NULL,
    finished_by TEXT NOT NULL,
    PRIMARY KEY (session_token, domain)
);
CREATE TABLE IF NOT EXISTS profile_fact_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT 'candidate',
    fact_key TEXT NOT NULL,
    value TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL,
    source_turn INTEGER,
    accepted INTEGER NOT NULL DEFAULT 1,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_token TEXT NOT NULL REFERENCES sessions(token),
    invite_code TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_token, doc_type, version)
);
CREATE INDEX IF NOT EXISTS idx_profile_facts_session
    ON profile_facts(session_token, domain);
CREATE INDEX IF NOT EXISTS idx_interview_turns_session
    ON interview_turns(session_token, domain);
CREATE INDEX IF NOT EXISTS idx_documents_session
    ON documents(session_token, doc_type);
CREATE INDEX IF NOT EXISTS idx_fact_history_session
    ON profile_fact_history(session_token, domain, fact_key);
"""


def db_path() -> Path:
    return Path(os.environ.get("CAREER_ADVISOR_DB", DEFAULT_DB_PATH))


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created before a column existed."""
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(interview_turns)")
    }
    if "target_key" not in cols:
        # Which profile slot a question is digging at. Older rows stay NULL,
        # so per-slot dig counts simply start from this migration forward.
        conn.execute("ALTER TABLE interview_turns ADD COLUMN target_key TEXT")
        conn.commit()


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_invite(conn: sqlite3.Connection, name: str) -> str:
    # 128-bit token: invite links are the standing credential until the
    # session cookie exchange, so they must not be guessable.
    code = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO invites (code, name, created_at) VALUES (?, ?, ?)",
        (code, name, utcnow()),
    )
    conn.commit()
    return code


def get_invite(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invites WHERE code = ? AND revoked = 0", (code,)
    ).fetchone()


def list_invites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM invites ORDER BY created_at").fetchall()


def revoke_invite(conn: sqlite3.Connection, code: str) -> bool:
    cur = conn.execute("UPDATE invites SET revoked = 1 WHERE code = ?", (code,))
    conn.commit()
    return cur.rowcount > 0


def create_session(conn: sqlite3.Connection, invite_code: str) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    conn.execute(
        "INSERT INTO sessions (token, invite_code, created_at, last_seen)"
        " VALUES (?, ?, ?, ?)",
        (token, invite_code, now, now),
    )
    conn.commit()
    return token


def get_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT s.token, s.invite_code, i.name FROM sessions s"
        " JOIN invites i ON i.code = s.invite_code"
        " WHERE s.token = ? AND i.revoked = 0",
        (token,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE sessions SET last_seen = ? WHERE token = ?",
            (utcnow(), token),
        )
        conn.commit()
    return row


# --- Profile facts -----------------------------------------------------------


def list_profile_facts(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM profile_facts"
        " WHERE session_token = ? AND domain = ? AND status != 'superseded'"
        " ORDER BY fact_key",
        (session_token, domain),
    ).fetchall()


def get_profile_fact(
    conn: sqlite3.Connection,
    session_token: str,
    fact_key: str,
    domain: str = "candidate",
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM profile_facts"
        " WHERE session_token = ? AND domain = ? AND fact_key = ?",
        (session_token, domain, fact_key),
    ).fetchone()


def record_fact_history(
    conn: sqlite3.Connection,
    *,
    session_token: str,
    fact_key: str,
    value: str,
    evidence: str = "",
    confidence: float | None = None,
    source_turn: int | None = None,
    accepted: bool = True,
    reason: str | None = None,
    domain: str = "candidate",
) -> None:
    """Append-only log of every extracted fact, kept or rejected.

    ``profile_facts`` holds one row per slot, so a later answer overwrites an
    earlier one. This table is how a discarded-but-better earlier value stays
    recoverable.
    """
    conn.execute(
        """
        INSERT INTO profile_fact_history (
            session_token, domain, fact_key, value, evidence, confidence,
            source_turn, accepted, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_token,
            domain,
            fact_key,
            value,
            evidence,
            confidence,
            source_turn,
            1 if accepted else 0,
            reason,
            utcnow(),
        ),
    )
    conn.commit()


def list_fact_history(
    conn: sqlite3.Connection,
    session_token: str,
    fact_key: str | None = None,
    domain: str = "candidate",
) -> list[sqlite3.Row]:
    if fact_key is None:
        return conn.execute(
            "SELECT * FROM profile_fact_history"
            " WHERE session_token = ? AND domain = ? ORDER BY id",
            (session_token, domain),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM profile_fact_history"
        " WHERE session_token = ? AND domain = ? AND fact_key = ?"
        " ORDER BY id",
        (session_token, domain, fact_key),
    ).fetchall()


def upsert_profile_fact(
    conn: sqlite3.Connection,
    *,
    session_token: str,
    invite_code: str,
    fact_key: str,
    value: str,
    evidence: str = "",
    confidence: float | None = None,
    source_turn: int | None = None,
    status: str = "active",
    domain: str = "candidate",
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO profile_facts (
            session_token, invite_code, domain, fact_key, value, evidence,
            confidence, source_turn, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_token, domain, fact_key) DO UPDATE SET
            value = excluded.value,
            evidence = excluded.evidence,
            confidence = excluded.confidence,
            source_turn = excluded.source_turn,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            session_token,
            invite_code,
            domain,
            fact_key,
            value,
            evidence,
            confidence,
            source_turn,
            status,
            now,
            now,
        ),
    )
    conn.commit()


def facts_as_dicts(
    rows: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# --- Interview turns ---------------------------------------------------------


def next_turn_index(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(turn_index), -1) AS m FROM interview_turns"
        " WHERE session_token = ? AND domain = ?",
        (session_token, domain),
    ).fetchone()
    return int(row["m"]) + 1


def list_interview_turns(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM interview_turns"
        " WHERE session_token = ? AND domain = ?"
        " ORDER BY turn_index",
        (session_token, domain),
    ).fetchall()


def recent_questions(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
    limit: int = 12,
) -> list[sqlite3.Row]:
    """Last ``limit`` questions asked, oldest first.

    The engine feeds these back to the model so it can see what it has
    already asked — without this it re-asks the same question indefinitely.
    """
    rows = conn.execute(
        "SELECT turn_index, question, section, target_key, answer"
        " FROM interview_turns WHERE session_token = ? AND domain = ?"
        " ORDER BY turn_index DESC LIMIT ?",
        (session_token, domain, limit),
    ).fetchall()
    return list(reversed(rows))


def dig_attempts(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> dict[str, int]:
    """How many questions have targeted each profile slot."""
    rows = conn.execute(
        "SELECT target_key, COUNT(*) AS n FROM interview_turns"
        " WHERE session_token = ? AND domain = ? AND target_key IS NOT NULL"
        " GROUP BY target_key",
        (session_token, domain),
    ).fetchall()
    return {row["target_key"]: int(row["n"]) for row in rows}


def get_open_turn(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> sqlite3.Row | None:
    """Latest turn that still needs an answer (answer IS NULL)."""
    return conn.execute(
        "SELECT * FROM interview_turns"
        " WHERE session_token = ? AND domain = ? AND answer IS NULL"
        " ORDER BY turn_index DESC LIMIT 1",
        (session_token, domain),
    ).fetchone()


def get_last_answered_turn(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM interview_turns"
        " WHERE session_token = ? AND domain = ? AND answer IS NOT NULL"
        " ORDER BY turn_index DESC LIMIT 1",
        (session_token, domain),
    ).fetchone()


def insert_interview_turn(
    conn: sqlite3.Connection,
    *,
    session_token: str,
    invite_code: str,
    turn_index: int,
    question: str,
    section: str | None = None,
    target_key: str | None = None,
    domain: str = "candidate",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO interview_turns (
            session_token, invite_code, domain, turn_index, question,
            answer, extraction_json, section, target_key, created_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
        """,
        (
            session_token,
            invite_code,
            domain,
            turn_index,
            question,
            section,
            target_key,
            utcnow(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def complete_interview_turn(
    conn: sqlite3.Connection,
    turn_id: int,
    answer: str,
    extraction: dict[str, Any] | None,
) -> None:
    conn.execute(
        "UPDATE interview_turns SET answer = ?, extraction_json = ?"
        " WHERE id = ?",
        (
            answer,
            json.dumps(extraction) if extraction is not None else None,
            turn_id,
        ),
    )
    conn.commit()


def finish_interview(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
    finished_by: str = "candidate",
) -> None:
    """Close the interview explicitly. ``finished_by``: candidate | turn_cap."""
    conn.execute(
        "INSERT INTO interview_state (session_token, domain, finished_at,"
        " finished_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(session_token, domain) DO NOTHING",
        (session_token, domain, utcnow(), finished_by),
    )
    conn.commit()


def interview_finished(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> sqlite3.Row | None:
    """The explicit-finish row, if the candidate or the turn cap ended it."""
    return conn.execute(
        "SELECT * FROM interview_state WHERE session_token = ? AND domain = ?",
        (session_token, domain),
    ).fetchone()


def interview_complete_flag(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = "candidate",
) -> bool:
    """True if the candidate ended it, or the latest extraction marked done."""
    if interview_finished(conn, session_token, domain) is not None:
        return True
    row = get_last_answered_turn(conn, session_token, domain)
    if row is None or not row["extraction_json"]:
        return False
    try:
        data = json.loads(row["extraction_json"])
    except json.JSONDecodeError:
        return False
    return bool(data.get("interview_complete"))


# --- Documents ---------------------------------------------------------------


def next_document_version(
    conn: sqlite3.Connection,
    session_token: str,
    doc_type: str,
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS m FROM documents"
        " WHERE session_token = ? AND doc_type = ?",
        (session_token, doc_type),
    ).fetchone()
    return int(row["m"]) + 1


def insert_document(
    conn: sqlite3.Connection,
    *,
    session_token: str,
    invite_code: str,
    doc_type: str,
    title: str,
    body_markdown: str,
) -> sqlite3.Row:
    version = next_document_version(conn, session_token, doc_type)
    cur = conn.execute(
        """
        INSERT INTO documents (
            session_token, invite_code, doc_type, version, title,
            body_markdown, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_token,
            invite_code,
            doc_type,
            version,
            title,
            body_markdown,
            utcnow(),
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM documents WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def list_documents(
    conn: sqlite3.Connection,
    session_token: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE session_token = ?"
        " ORDER BY doc_type, version DESC",
        (session_token,),
    ).fetchall()


def list_documents_by_type(
    conn: sqlite3.Connection,
    session_token: str,
    doc_type: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE session_token = ? AND doc_type = ?"
        " ORDER BY version DESC",
        (session_token, doc_type),
    ).fetchall()


def get_document(
    conn: sqlite3.Connection,
    doc_id: int,
    session_token: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE id = ? AND session_token = ?",
        (doc_id, session_token),
    ).fetchone()
