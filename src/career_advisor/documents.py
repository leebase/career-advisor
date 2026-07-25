"""Document generator — resume and job-search strategies from profile facts.

Documents are produced from stored profile facts only (never raw chat
transcripts). Each generation is a new version; prior versions remain
viewable and downloadable.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db, llm
from .profile import facts_as_prompt_block, load_schema

DOC_RESUME = "resume"
DOC_STRATEGY = "job_search_strategy"

DOC_TYPES = {
    DOC_RESUME: {
        "title": "Resume",
        "heading": "Professional Resume",
    },
    DOC_STRATEGY: {
        "title": "Job Search Strategy",
        "heading": "Job Search Strategies Document",
    },
}


def _profile_block(conn: sqlite3.Connection, session_token: str, domain: str) -> str:
    schema = load_schema(domain)
    facts = db.facts_as_dicts(db.list_profile_facts(conn, session_token, domain))
    if not facts:
        return ""
    return facts_as_prompt_block(schema, facts)


def _resume_prompt(profile_block: str, candidate_name: str) -> str:
    return f"""You are the Resume Architect for Career Advisor.
Build a clean, ATS-friendly resume in Markdown from the evidenced profile facts
below. Use ONLY these facts — do not invent employers, dates, metrics, or skills.
If a section lacks facts, omit it or keep it brief with honest placeholders like
"(add detail in interview)".

Candidate display name: {candidate_name}

PROFILE FACTS:
{profile_block}

Output Markdown only (no JSON, no preamble). Structure:
# {{Name}}
contact / location line if known

## Summary
## Experience
## Skills
## Achievements
## Education & Certifications
## Preferences (brief, optional)

Write strong action bullets grounded in evidence. Prefer metrics that appear
in the facts. Professional tone.
"""


def _strategy_prompt(profile_block: str, candidate_name: str) -> str:
    return f"""You are the Career Coach for Career Advisor.
Write a practical job-search strategies document in Markdown for {candidate_name},
using ONLY the profile facts below. Do not invent employers or credentials.

PROFILE FACTS:
{profile_block}

Output Markdown only. Include:
# Job Search Strategy for {{Name}}
## Target roles and positioning
## Where to look (boards, communities, company types — realistic
   free/API-friendly sources; no scraping advice)
## Who to contact (recruiters, peers, communities — types, not fake people)
## Weekly plan (concrete actions Mon–Sun style or weekly cadence)
## Materials checklist (resume versions, LinkedIn, portfolio)
## Risks and focus for the next 30 days

Be specific to their stack and goals. Actionable, phone-readable.
"""


def generate_document(
    conn: sqlite3.Connection,
    *,
    session_token: str,
    invite_code: str,
    doc_type: str,
    candidate_name: str,
    domain: str = "candidate",
    complete_fn=None,
) -> tuple[sqlite3.Row | None, str | None]:
    """Generate a new document version. Returns (row, error)."""
    if doc_type not in DOC_TYPES:
        return None, f"Unknown document type: {doc_type}"

    profile_block = _profile_block(conn, session_token, domain)
    if not profile_block:
        return None, (
            "No profile facts yet. Complete some of the interview first."
        )

    complete = complete_fn or llm.complete
    if doc_type == DOC_RESUME:
        prompt = _resume_prompt(profile_block, candidate_name)
        title = f"{DOC_TYPES[doc_type]['heading']} — {candidate_name}"
    else:
        prompt = _strategy_prompt(profile_block, candidate_name)
        title = f"{DOC_TYPES[doc_type]['heading']} — {candidate_name}"

    try:
        body = complete(prompt).strip()
    except llm.LLMError as exc:
        return None, f"Document generation failed: {exc}"

    if not body:
        return None, "Model returned an empty document."

    row = db.insert_document(
        conn,
        session_token=session_token,
        invite_code=invite_code,
        doc_type=doc_type,
        title=title,
        body_markdown=body,
    )
    return row, None


def documents_summary(
    conn: sqlite3.Connection, session_token: str
) -> dict[str, list[dict[str, Any]]]:
    """Group documents by type for the UI, newest first within type."""
    rows = db.list_documents(conn, session_token)
    out: dict[str, list[dict[str, Any]]] = {
        DOC_RESUME: [],
        DOC_STRATEGY: [],
    }
    for row in rows:
        dtype = row["doc_type"]
        if dtype not in out:
            out[dtype] = []
        out[dtype].append(dict(row))
    return out
