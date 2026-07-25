"""Interview session review — read a real candidate's session back.

Built because diagnosing the first live session (78 turns, "it asks the same
questions multiple times") took hand-written SQLite queries against the
production database, twice. The health of an interview is the product's whole
value, so reading one back should be a command, not an investigation.

Surfaces what the transcript alone does not: how often questions repeat, how
engagement decays, and how many turns it cost to capture each fact.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import db
from .profile import load_schema

DOMAIN = "candidate"

# Ignore words carry no signal about what a question is actually asking.
_STOPWORDS = frozenset(
    """a about across all an and any are as at be been but by can did do does
    for from had has have how i in is it its like me more most much my of on
    one or our out over roughly so some than that the their them then there
    these they this those to under up was we were what when where which who
    whom why will with would you your yours""".split()
)

# Two questions sharing this fraction of their meaningful words are asking the
# same thing, however differently they are worded.
REPEAT_SIMILARITY = 0.6


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z']+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class RepeatPair:
    first_index: int
    second_index: int
    score: float
    first: str
    second: str


@dataclass
class SessionReview:
    session_token: str
    name: str
    turn_count: int
    answered: int
    unanswered: int
    fact_count: int
    finished_by: str | None
    turns_per_fact: float
    repeats: list[RepeatPair] = field(default_factory=list)
    common_phrases: list[tuple[str, int]] = field(default_factory=list)
    first_answers_avg: int = 0
    last_answers_avg: int = 0
    coverage_summary: dict[str, Any] = field(default_factory=dict)
    frustration: list[tuple[int, str]] = field(default_factory=list)

    @property
    def repeat_rate(self) -> float:
        if self.turn_count < 2:
            return 0.0
        return len(self.repeats) / self.turn_count


# Things a candidate says when the engine has stopped listening. Finding any
# of these in a transcript is a product failure, not a data point.
_FRUSTRATION = (
    "already answered",
    "already told you",
    "asked me that",
    "see previous",
    "as i said",
    "enough yet",
    "enough for a resume",
    "enough information",
    "same question",
    "again?",
)


def _avg_answer_length(rows: list[sqlite3.Row]) -> int:
    lengths = [len(r["answer"] or "") for r in rows if r["answer"]]
    return int(sum(lengths) / len(lengths)) if lengths else 0


def review_session(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = DOMAIN,
) -> SessionReview:
    """Compute interview-health metrics for one session."""
    turns = db.list_interview_turns(conn, session_token, domain)
    facts = db.facts_as_dicts(db.list_profile_facts(conn, session_token, domain))
    session = conn.execute(
        "SELECT i.name FROM sessions s JOIN invites i ON i.code = s.invite_code"
        " WHERE s.token = ?",
        (session_token,),
    ).fetchone()
    finished = db.interview_finished(conn, session_token, domain)

    keyword_sets = [(t["turn_index"], _keywords(t["question"])) for t in turns]
    by_index = {t["turn_index"]: t["question"] for t in turns}
    repeats: list[RepeatPair] = []
    for i, (idx_a, words_a) in enumerate(keyword_sets):
        for idx_b, words_b in keyword_sets[i + 1 :]:
            score = _similarity(words_a, words_b)
            if score >= REPEAT_SIMILARITY:
                repeats.append(
                    RepeatPair(
                        first_index=idx_a,
                        second_index=idx_b,
                        score=round(score, 2),
                        first=by_index[idx_a],
                        second=by_index[idx_b],
                    )
                )
    repeats.sort(key=lambda r: (-r.score, r.first_index))

    phrases: Counter[str] = Counter()
    for turn in turns:
        for word in _keywords(turn["question"]):
            phrases[word] += 1

    answered = [t for t in turns if (t["answer"] or "").strip()]
    frustration = [
        (t["turn_index"], (t["answer"] or "").strip())
        for t in turns
        if any(marker in (t["answer"] or "").lower() for marker in _FRUSTRATION)
    ]

    schema = load_schema(domain)
    from .interview import _load_state  # local import avoids a cycle

    _s, _f, cov = _load_state(conn, session_token, domain)

    return SessionReview(
        session_token=session_token,
        name=session["name"] if session else "(unknown)",
        turn_count=len(turns),
        answered=len(answered),
        unanswered=len(turns) - len(answered),
        fact_count=len(facts),
        finished_by=finished["finished_by"] if finished else None,
        turns_per_fact=round(len(turns) / len(facts), 1) if facts else 0.0,
        repeats=repeats,
        common_phrases=phrases.most_common(8),
        first_answers_avg=_avg_answer_length(turns[:10]),
        last_answers_avg=_avg_answer_length(turns[-10:]),
        coverage_summary={
            "filled": len(cov.filled),
            "weak": len(cov.weak),
            "empty": len(cov.empty),
            "accepted": len(cov.accepted),
            "skipped": len(cov.skipped),
            "open_gaps": len(cov.open_gaps),
            "ready_for_documents": cov.ready_for_documents(schema),
        },
        frustration=frustration,
    )


def list_sessions(conn: sqlite3.Connection, domain: str = DOMAIN) -> list[dict]:
    """Every session with interview activity, busiest first."""
    rows = conn.execute(
        "SELECT t.session_token, COUNT(*) AS turns, i.name"
        " FROM interview_turns t"
        " JOIN sessions s ON s.token = t.session_token"
        " JOIN invites i ON i.code = s.invite_code"
        " WHERE t.domain = ?"
        " GROUP BY t.session_token, i.name ORDER BY turns DESC",
        (domain,),
    ).fetchall()
    return [dict(r) for r in rows]


def format_review(review: SessionReview, transcript: bool = False,
                  turns: list[sqlite3.Row] | None = None) -> str:
    """Render a review as operator-readable text."""
    lines = [
        f"Session {review.session_token[:10]}…  ({review.name})",
        f"  turns: {review.turn_count}"
        f" ({review.answered} answered, {review.unanswered} skipped)",
        f"  facts: {review.fact_count}"
        f"  —  {review.turns_per_fact} turns per fact",
        f"  ended: {review.finished_by or 'still open'}",
        "",
        f"  REPEATED QUESTIONS: {len(review.repeats)}"
        f" pairs ({review.repeat_rate:.0%} of turns)",
    ]
    for pair in review.repeats[:10]:
        lines.append(f"    Q{pair.first_index} ≈ Q{pair.second_index} ({pair.score})")
        lines.append(f"      {pair.first}")
        lines.append(f"      {pair.second}")
    if len(review.repeats) > 10:
        lines.append(f"    … and {len(review.repeats) - 10} more pairs")

    lines += [
        "",
        "  MOST-USED QUESTION WORDS: "
        + ", ".join(f"{w} ×{n}" for w, n in review.common_phrases),
        "",
        f"  ENGAGEMENT: first 10 answers avg {review.first_answers_avg} chars,"
        f" last 10 avg {review.last_answers_avg} chars",
    ]
    if review.first_answers_avg and review.last_answers_avg < (
        review.first_answers_avg / 2
    ):
        lines.append("    ⚠ answers shrank by more than half — candidate disengaged")

    if review.frustration:
        lines += ["", "  ⚠ CANDIDATE PUSHED BACK:"]
        for index, text in review.frustration:
            lines.append(f"    Q{index}: {text[:120]}")

    cov = review.coverage_summary
    lines += [
        "",
        f"  COVERAGE: {cov['filled']} filled, {cov['accepted']} accepted,"
        f" {cov['weak']} weak, {cov['empty']} empty"
        f"  ({cov['open_gaps']} open gaps)",
        f"  READY FOR DOCUMENTS: {cov['ready_for_documents']}",
    ]

    if transcript and turns is not None:
        lines += ["", "  TRANSCRIPT:"]
        for turn in turns:
            answer = (turn["answer"] or "(unanswered)").replace("\n", " ")
            lines.append(f"    Q{turn['turn_index']} [{turn['section'] or '-'}]"
                         f" {turn['question']}")
            lines.append(f"      → {answer[:200]}")

    return "\n".join(lines)
