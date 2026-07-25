"""Quality Interview Engine — schema-driven, evidenced fact collection.

Not a free-form chat loop. Each turn is one ``llm.complete`` call that
returns validated JSON: extracted facts + one follow-up question. Digging
doctrine: weak/unevidenced answers get depth follow-ups before the engine
moves on. State lives in SQLite so the browser can close and resume.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db, llm
from .profile import (
    DomainSchema,
    coverage,
    facts_as_prompt_block,
    is_degradation,
    load_schema,
    schema_as_prompt_block,
)

DOMAIN = "candidate"

# Ask about one slot at most this many times, then take what we have and move
# on. The first real user was asked "what did you personally own day to day"
# fifteen times across 78 turns; two slots consumed the last third of their
# interview because nothing capped the digging (see feedback.md 2026-07-24).
MAX_DIGS_PER_SLOT = 2

# Questions already asked that get shown back to the model each turn.
QUESTION_MEMORY = 12

# Past this many turns the UI pushes the finish option; past the hard cap the
# engine closes the interview itself. No interview should ever run to 78.
SOFT_TURN_NUDGE = 20
HARD_TURN_CAP = 40

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


@dataclass
class TurnResult:
    question: str | None
    section: str | None
    interview_complete: bool
    extraction: dict[str, Any] | None
    coverage_report: Any
    error: str | None = None
    # What the answer just bought, rendered back to the candidate so the
    # interview stops feeling like talking into a void.
    captured: list[dict[str, Any]] = field(default_factory=list)
    kept_earlier: list[dict[str, Any]] = field(default_factory=list)
    rationale: str | None = None


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse model output into a dict; raise ValueError on failure."""
    cleaned = text.strip()
    fence = _JSON_FENCE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    # Try whole string, then first {...} slice.
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    last_err: Exception | None = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError as exc:
            last_err = exc
    raise ValueError(f"Model output is not valid JSON: {last_err}")


def _validate_extraction(
    data: dict[str, Any], schema: DomainSchema
) -> dict[str, Any]:
    """Normalize and validate model JSON. Raises ValueError if unusable."""
    if not isinstance(data, dict):
        raise ValueError("extraction root must be an object")

    complete = bool(data.get("interview_complete"))
    follow_up = data.get("follow_up_question")
    if follow_up is not None:
        follow_up = str(follow_up).strip() or None
    if not complete and not follow_up:
        raise ValueError(
            "follow_up_question required unless interview_complete is true"
        )

    section = data.get("section")
    section = str(section).strip() if section else None
    rationale = str(data.get("rationale") or "").strip()

    slot_map_all = schema.slot_map()
    target_key = data.get("target_fact_key") or data.get("target_key")
    target_key = str(target_key).strip() if target_key else None
    if target_key not in slot_map_all:
        # Unusable target just means this question is not counted against a
        # slot; never reject the turn over it.
        target_key = None

    raw_facts = data.get("extracted_facts") or []
    if not isinstance(raw_facts, list):
        raise ValueError("extracted_facts must be a list")

    slot_map = schema.slot_map()
    facts: list[dict[str, Any]] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("fact_key") or "").strip()
        if not key or key not in slot_map:
            # Skip unknown keys rather than reject the whole turn.
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        evidence = str(item.get("evidence") or "").strip()
        conf_raw = item.get("confidence")
        try:
            confidence = float(conf_raw) if conf_raw is not None else 0.7
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))
        status = str(item.get("status") or "active")
        if status not in ("active", "contradicted"):
            status = "active"
        facts.append(
            {
                "key": key,
                "value": value,
                "evidence": evidence,
                "confidence": confidence,
                "status": status,
            }
        )

    return {
        "extracted_facts": facts,
        "follow_up_question": follow_up,
        "section": section,
        "target_fact_key": target_key,
        "rationale": rationale,
        "interview_complete": complete,
    }


def _asked_block(asked: list[Any]) -> str:
    """Render previously asked questions for the prompt."""
    if not asked:
        return "(nothing asked yet)"
    lines = []
    for row in asked:
        answered = (row["answer"] or "").strip()
        state = "answered" if answered else "skipped"
        lines.append(f'- Q{row["turn_index"]} ({state}): {row["question"]}')
    return "\n".join(lines)


def _build_prompt(
    schema: DomainSchema,
    facts: list[dict[str, Any]],
    cov,
    latest_answer: str | None,
    is_opening: bool,
    asked: list[Any] | None = None,
) -> str:
    gaps = cov.open_gaps
    gap_detail = []
    slot_map = schema.slot_map()
    for key in gaps[:20]:
        slot = slot_map.get(key)
        if slot:
            state = (
                "empty"
                if key in cov.empty
                else "weak"
                if key in cov.weak
                else "contradicted"
            )
            gap_detail.append(
                f"- {key} ({state}) [{slot.priority}]: {slot.description}"
                f" | need: {slot.evidence}"
            )

    required_open = [
        k
        for k in gaps
        if slot_map.get(k) and slot_map[k].priority == "required"
    ]

    settled = list(cov.accepted) + list(cov.skipped)

    mode = (
        "OPENING: No prior answer. Greet briefly and ask the single best first "
        "question to start filling the candidate profile (usually name + role "
        "target or current situation)."
        if is_opening
        else (
            "CONTINUATION: The user just answered. Extract facts with evidence, "
            "then ask exactly ONE follow-up.\n"
            f"User's latest answer:\n\"\"\"\n{latest_answer}\n\"\"\""
        )
    )

    return f"""You are the Career Interviewer for Career Advisor.
You collect evidenced facts into a Domain Profile. You are NOT a chatbot —
every turn must advance structured coverage.

DIGGING DOCTRINE:
- Never accept surface answers for skills, systems, or achievements.
- If the user names a tool/system without metrics or ownership, dig:
  volume? scale? what did you automate? who escalated to you? measurable result?
- Prefer depth on weak slots before opening brand-new optional slots.
- One question only. Short, conversational, phone-friendly.

NEVER REPEAT YOURSELF:
- The questions you have already asked are listed below. Do NOT ask any of
  them again, and do not ask a reworded version of one. "What did you own
  day to day?" and "which systems were you responsible for?" are the SAME
  question — asking it twice is the single worst thing you can do here.
- You get at most {MAX_DIGS_PER_SLOT} questions per profile slot. If a slot
  has been asked about that often, take the best answer you have and move to
  a different slot. Settled slots are listed below — treat them as closed.
- If the candidate says they already answered something, or asks whether you
  have enough, believe them: stop digging that slot and either move on or set
  interview_complete.
- Never tell the candidate you cannot see earlier answers. You can — the
  facts and questions below are your memory.

QUESTIONS ALREADY ASKED (do not repeat or reword these):
{_asked_block(asked or [])}

{mode}

SCHEMA:
{schema_as_prompt_block(schema)}

FACTS COLLECTED SO FAR:
{facts_as_prompt_block(schema, facts)}

OPEN GAPS (empty / weak / contradicted):
{chr(10).join(gap_detail) if gap_detail else "(none — profile looks strong)"}

SETTLED — dug enough already, never ask about these again:
{", ".join(settled) or "(none)"}

Required slots still open: {", ".join(required_open) or "none"}

Respond with JSON ONLY (no markdown, no prose outside JSON). Shape:
{{
  "extracted_facts": [
    {{
      "key": "section.fact_id",
      "value": "concise fact text",
      "evidence": "proof: services, scale, ownership, outcomes",
      "confidence": 0.0,
      "status": "active"
    }}
  ],
  "follow_up_question": "one question string or null if complete",
  "section": "section_id for the question",
  "target_fact_key": "section.fact_id the question is digging at",
  "rationale": "why this question, in one short line the candidate could read",
  "interview_complete": false
}}

Rules for JSON:
- Use only schema fact keys (section.fact_id).
- target_fact_key must be a real schema key and must not be a settled slot.
- rationale is shown to the candidate — write it for them, not for a log.
- extracted_facts may be [] on the opening turn.
- Set interview_complete true only when required slots are filled or
  strongly evidenced and remaining gaps are optional/preferred you choose
  to leave. Prefer completing required + preferred achievements first.
- If still digging, interview_complete must be false and follow_up_question
  must be a non-empty string.
"""


def _corrective_prompt(bad_output: str) -> str:
    return (
        "Your previous response was not valid JSON matching the required "
        "schema. Reply again with JSON ONLY, no markdown fences, shape:\n"
        '{"extracted_facts":[],"follow_up_question":"...","section":"...",'
        '"rationale":"...","interview_complete":false}\n\n'
        f"Previous output was:\n{bad_output[:2000]}"
    )


def call_model_for_turn(
    schema: DomainSchema,
    facts: list[dict[str, Any]],
    cov,
    latest_answer: str | None,
    is_opening: bool,
    complete_fn=None,
    asked: list[Any] | None = None,
) -> dict[str, Any]:
    """One LLM turn with one parse retry. ``complete_fn`` injectable for tests."""
    complete = complete_fn or llm.complete
    prompt = _build_prompt(schema, facts, cov, latest_answer, is_opening, asked)
    raw = complete(prompt)
    try:
        data = _extract_json_object(raw)
        return _validate_extraction(data, schema)
    except ValueError:
        raw2 = complete(_corrective_prompt(raw))
        data = _extract_json_object(raw2)
        return _validate_extraction(data, schema)


def _apply_facts(
    conn: sqlite3.Connection,
    session_token: str,
    invite_code: str,
    turn_index: int,
    extraction: dict[str, Any],
    schema: DomainSchema,
    domain: str = DOMAIN,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Store extracted facts. Returns (captured, kept_earlier) for the UI."""
    slot_map = schema.slot_map()
    captured: list[dict[str, Any]] = []
    kept_earlier: list[dict[str, Any]] = []

    for fact in extraction.get("extracted_facts") or []:
        key = fact["key"]
        slot = slot_map.get(key)
        existing = db.get_profile_fact(conn, session_token, key, domain)
        blocked, reason = is_degradation(existing, fact)

        db.record_fact_history(
            conn,
            session_token=session_token,
            fact_key=key,
            value=fact["value"],
            evidence=fact.get("evidence") or "",
            confidence=fact.get("confidence"),
            source_turn=turn_index,
            accepted=not blocked,
            reason=reason or None,
            domain=domain,
        )

        label = slot.description if slot else key
        section_title = slot.section_title if slot else ""
        if blocked:
            kept_earlier.append(
                {
                    "key": key,
                    "label": label,
                    "section_title": section_title,
                    "kept": existing["value"] if existing else "",
                    "reason": reason,
                }
            )
            continue

        db.upsert_profile_fact(
            conn,
            session_token=session_token,
            invite_code=invite_code,
            fact_key=key,
            value=fact["value"],
            evidence=fact.get("evidence") or "",
            confidence=fact.get("confidence"),
            source_turn=turn_index,
            status=fact.get("status") or "active",
            domain=domain,
        )
        captured.append(
            {
                "key": key,
                "label": label,
                "section_title": section_title,
                "value": fact["value"],
                "evidence": fact.get("evidence") or "",
                "confidence": fact.get("confidence"),
                "updated": existing is not None,
            }
        )

    return captured, kept_earlier


def _exhausted_keys(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = DOMAIN,
) -> set[str]:
    """Slots already asked about ``MAX_DIGS_PER_SLOT`` times."""
    return {
        key
        for key, count in db.dig_attempts(conn, session_token, domain).items()
        if count >= MAX_DIGS_PER_SLOT
    }


def _load_state(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = DOMAIN,
) -> tuple[DomainSchema, list[dict[str, Any]], Any]:
    schema = load_schema(domain)
    rows = db.list_profile_facts(conn, session_token, domain)
    facts = db.facts_as_dicts(rows)
    cov = coverage(
        schema,
        facts,
        exhausted_keys=_exhausted_keys(conn, session_token, domain),
    )
    return schema, facts, cov


def ensure_opening_question(
    conn: sqlite3.Connection,
    session_token: str,
    invite_code: str,
    domain: str = DOMAIN,
    complete_fn=None,
) -> TurnResult:
    """Resume open question, or create the first question if none exist."""
    schema, facts, cov = _load_state(conn, session_token, domain)

    # Completion is checked before the open turn: a candidate who hits finish
    # while a question is on screen must not have it handed back to them.
    if db.interview_complete_flag(conn, session_token, domain):
        return TurnResult(
            question=None,
            section=None,
            interview_complete=True,
            extraction=None,
            coverage_report=cov,
        )

    open_turn = db.get_open_turn(conn, session_token, domain)
    if open_turn is not None:
        return TurnResult(
            question=open_turn["question"],
            section=open_turn["section"],
            interview_complete=False,
            extraction=None,
            coverage_report=cov,
        )

    turns = db.list_interview_turns(conn, session_token, domain)
    if len(turns) >= HARD_TURN_CAP:
        db.finish_interview(conn, session_token, domain, finished_by="turn_cap")
        return TurnResult(
            question=None,
            section=None,
            interview_complete=True,
            extraction=None,
            coverage_report=cov,
        )

    if turns:
        # All turns answered but not marked complete — ask model for next Q.
        last = turns[-1]
        latest_answer = last["answer"] or ""
        asked = db.recent_questions(
            conn, session_token, domain, limit=QUESTION_MEMORY
        )
        try:
            extraction = call_model_for_turn(
                schema,
                facts,
                cov,
                latest_answer,
                is_opening=False,
                complete_fn=complete_fn,
                asked=asked,
            )
        except (ValueError, llm.LLMError) as exc:
            return TurnResult(
                question=None,
                section=None,
                interview_complete=False,
                extraction=None,
                coverage_report=cov,
                error=str(exc),
            )
        # No new user answer this call; only use extraction for next question.
        if extraction.get("interview_complete") and not extraction.get(
            "follow_up_question"
        ):
            # Persist a synthetic complete marker on last turn's extraction.
            return TurnResult(
                question=None,
                section=None,
                interview_complete=True,
                extraction=extraction,
                coverage_report=cov,
            )
        turn_index = db.next_turn_index(conn, session_token, domain)
        db.insert_interview_turn(
            conn,
            session_token=session_token,
            invite_code=invite_code,
            turn_index=turn_index,
            question=extraction["follow_up_question"],
            section=extraction.get("section"),
            target_key=extraction.get("target_fact_key"),
            domain=domain,
        )
        return TurnResult(
            question=extraction["follow_up_question"],
            section=extraction.get("section"),
            interview_complete=False,
            extraction=extraction,
            coverage_report=cov,
            rationale=extraction.get("rationale") or None,
        )

    # True opening.
    try:
        extraction = call_model_for_turn(
            schema, facts, cov, None, is_opening=True, complete_fn=complete_fn
        )
    except (ValueError, llm.LLMError) as exc:
        # Deterministic fallback so the UI never dies if the model is down.
        fallback_q = (
            "To start building your profile: what's your full name, and what "
            "kind of role are you aiming for next?"
        )
        turn_index = db.next_turn_index(conn, session_token, domain)
        db.insert_interview_turn(
            conn,
            session_token=session_token,
            invite_code=invite_code,
            turn_index=turn_index,
            question=fallback_q,
            section="identity",
            domain=domain,
        )
        return TurnResult(
            question=fallback_q,
            section="identity",
            interview_complete=False,
            extraction=None,
            coverage_report=cov,
            error=str(exc),
        )

    question = extraction.get("follow_up_question") or (
        "What's your full name, and what kind of role are you aiming for next?"
    )
    turn_index = db.next_turn_index(conn, session_token, domain)
    db.insert_interview_turn(
        conn,
        session_token=session_token,
        invite_code=invite_code,
        turn_index=turn_index,
        question=question,
        section=extraction.get("section") or "identity",
        target_key=extraction.get("target_fact_key"),
        domain=domain,
    )
    return TurnResult(
        question=question,
        section=extraction.get("section"),
        interview_complete=bool(extraction.get("interview_complete")),
        extraction=extraction,
        coverage_report=cov,
        rationale=extraction.get("rationale") or None,
    )


def submit_answer(
    conn: sqlite3.Connection,
    session_token: str,
    invite_code: str,
    answer: str,
    domain: str = DOMAIN,
    complete_fn=None,
) -> TurnResult:
    """Record the user's answer, extract facts, queue the next question."""
    answer = (answer or "").strip()
    if not answer:
        schema, facts, cov = _load_state(conn, session_token, domain)
        open_turn = db.get_open_turn(conn, session_token, domain)
        return TurnResult(
            question=open_turn["question"] if open_turn else None,
            section=open_turn["section"] if open_turn else None,
            interview_complete=False,
            extraction=None,
            coverage_report=cov,
            error="Please share a bit more so we can capture it.",
        )

    schema, facts, cov = _load_state(conn, session_token, domain)
    if db.interview_complete_flag(conn, session_token, domain):
        # A stale tab posting after the candidate finished must not restart it.
        return TurnResult(
            question=None,
            section=None,
            interview_complete=True,
            extraction=None,
            coverage_report=cov,
        )

    open_turn = db.get_open_turn(conn, session_token, domain)
    if open_turn is None:
        # Start or recover.
        ensure_opening_question(
            conn, session_token, invite_code, domain, complete_fn
        )
        open_turn = db.get_open_turn(conn, session_token, domain)
        if open_turn is None:
            return TurnResult(
                question=None,
                section=None,
                interview_complete=db.interview_complete_flag(
                    conn, session_token, domain
                ),
                extraction=None,
                coverage_report=cov,
                error="No open interview question.",
            )

    asked = db.recent_questions(
        conn, session_token, domain, limit=QUESTION_MEMORY
    )
    try:
        extraction = call_model_for_turn(
            schema,
            facts,
            cov,
            answer,
            is_opening=False,
            complete_fn=complete_fn,
            asked=asked,
        )
    except (ValueError, llm.LLMError) as exc:
        return TurnResult(
            question=open_turn["question"],
            section=open_turn["section"],
            interview_complete=False,
            extraction=None,
            coverage_report=cov,
            error=f"Could not process that answer ({exc}). Try again.",
        )

    turn_index = int(open_turn["turn_index"])
    db.complete_interview_turn(conn, int(open_turn["id"]), answer, extraction)
    captured, kept_earlier = _apply_facts(
        conn, session_token, invite_code, turn_index, extraction, schema, domain
    )

    schema, facts, cov = _load_state(conn, session_token, domain)

    if extraction.get("interview_complete") or not extraction.get(
        "follow_up_question"
    ):
        # Mark complete on the extraction already stored.
        return TurnResult(
            question=None,
            section=None,
            interview_complete=True,
            extraction=extraction,
            coverage_report=cov,
            captured=captured,
            kept_earlier=kept_earlier,
        )

    turn_count = len(db.list_interview_turns(conn, session_token, domain))
    if turn_count >= HARD_TURN_CAP:
        # Stop here rather than queue question 41. Whatever is still open is
        # not worth what another question costs the candidate.
        db.finish_interview(conn, session_token, domain, finished_by="turn_cap")
        return TurnResult(
            question=None,
            section=None,
            interview_complete=True,
            extraction=extraction,
            coverage_report=cov,
            captured=captured,
            kept_earlier=kept_earlier,
        )

    next_index = db.next_turn_index(conn, session_token, domain)
    db.insert_interview_turn(
        conn,
        session_token=session_token,
        invite_code=invite_code,
        turn_index=next_index,
        question=extraction["follow_up_question"],
        section=extraction.get("section"),
        target_key=extraction.get("target_fact_key"),
        domain=domain,
    )
    return TurnResult(
        question=extraction["follow_up_question"],
        section=extraction.get("section"),
        interview_complete=False,
        extraction=extraction,
        coverage_report=cov,
        captured=captured,
        kept_earlier=kept_earlier,
        rationale=extraction.get("rationale") or None,
    )


def finish_now(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = DOMAIN,
) -> TurnResult:
    """Candidate-initiated end. "I've given you enough" must be a real exit."""
    db.finish_interview(conn, session_token, domain, finished_by="candidate")
    schema, facts, cov = _load_state(conn, session_token, domain)
    return TurnResult(
        question=None,
        section=None,
        interview_complete=True,
        extraction=None,
        coverage_report=cov,
    )


def progress_context(
    conn: sqlite3.Connection,
    session_token: str,
    domain: str = DOMAIN,
) -> dict[str, Any]:
    schema, facts, cov = _load_state(conn, session_token, domain)
    turn_count = len(db.list_interview_turns(conn, session_token, domain))
    return {
        "schema": schema,
        "facts": facts,
        "coverage": cov,
        "sections": cov.section_progress(schema),
        "turn_count": turn_count,
        "complete": db.interview_complete_flag(conn, session_token, domain),
        "ready": cov.ready_for_documents(schema),
        "nudge_finish": turn_count >= SOFT_TURN_NUDGE,
        "turns_remaining": max(0, HARD_TURN_CAP - turn_count),
    }
