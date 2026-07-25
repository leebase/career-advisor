# Architecture

Server-rendered FastAPI + Jinja2, SQLite for state, one pluggable LLM seam.
No front-end build step. Every route is served under a `/career-advisor/`
prefix by the app itself, so direct access and reverse-proxied access behave
identically.

```
Browser
   │
   ▼
nginx (optional, shared static webroot)
   │  /career-advisor/  →  proxy_pass
   ▼
FastAPI app  (src/career_advisor/web.py)
   │
   ├── Profile Engine       profile.py    schema loading + slot coverage
   ├── Interview Engine     interview.py  one JSON turn per answer
   ├── Document Generator   documents.py  versioned resume / strategy
   ├── Session review       review.py     interview health metrics
   └── Persistence          db.py         SQLite
                                │
                                ▼
                          llm.complete(prompt) -> str
                          (pluggable provider seam)
```

## Modules

| Module | Responsibility |
|---|---|
| `web.py` | Routes: invite exchange, home, interview GET/POST, finish, documents. Session cookie handling, invite rate limiting. |
| `db.py` | SQLite schema, additive migrations, all queries. Invites, sessions, profile facts, fact history, interview turns, interview state, documents. |
| `profile.py` | Domain-agnostic schema loading and coverage classification. Knows nothing about "candidate" beyond it being a schema file. |
| `interview.py` | The interview loop: prompt construction, JSON validation, fact application, dig caps, turn bounds. |
| `documents.py` | Resume and job-search strategy generation from stored facts. |
| `review.py` | Post-hoc interview health: repeat detection, engagement decay, coverage, pushback. |
| `llm.py` | The single provider seam. One contract: `complete(prompt) -> str`. |
| `main.py` | Operator CLI: invites, serve, review-session, LLM smoke test. |

## Data model

```
invites(code, name, created_at, revoked)
   │
   ▼
sessions(token, invite_code, created_at, last_seen)
   │
   ├── interview_turns(turn_index, question, answer, extraction_json,
   │                   section, target_key)
   │        one row per question; target_key names the slot it digs at,
   │        which is what makes per-slot dig caps possible
   │
   ├── profile_facts(fact_key, value, evidence, confidence, source_turn,
   │                 status)          UNIQUE(session, domain, fact_key)
   │        one row per slot — the current best answer
   │
   ├── profile_fact_history(fact_key, value, evidence, confidence,
   │                        accepted, reason)
   │        append-only; every extraction, including rejected ones
   │
   ├── interview_state(finished_at, finished_by)
   │        explicit end: 'candidate' or 'turn_cap'
   │
   └── documents(doc_type, version, title, body_markdown)
```

`profile_facts` is one row per slot, so a later answer replaces an earlier one.
That is why `profile_fact_history` exists: a strictly weaker answer is refused
rather than written, and every extraction stays recoverable either way.

Migrations are additive and run on connect (`db._migrate`), so an existing
database picks up new columns without a manual step.

## The interview loop

One model call per answer. The prompt carries:

- the domain schema, with each slot's description and required evidence
- facts collected so far, with their evidence and confidence
- open gaps, classified `empty` / `weak` / `contradicted`
- **settled slots** — dug at the cap already, never to be asked about again
- **the last 12 questions asked**, with an explicit no-repeat/no-reword rule
- the candidate's latest answer

The model returns validated JSON: extracted facts (each keyed to a real schema
slot, with evidence and confidence), one follow-up question, the slot that
question targets, a rationale written for the candidate to read, and an
`interview_complete` flag. Unparseable output gets one corrective retry; an
unknown target slot is dropped rather than failing the turn.

### Coverage classification

`profile.classify_fact` sorts each slot into `empty`, `weak`, `filled` or
`contradicted`. Two rules matter:

- Confidence below 0.4 is always weak.
- Confidence at or above `CONFIDENCE_SATISFIES` (0.75) **with evidence text**
  is filled, even with no number in the value.

That second rule is load-bearing. Without it, a metric heuristic — "no digit,
no metric word, therefore weak" — keeps confidently-evidenced qualitative
answers permanently in `open_gaps`, and an engine told to prefer weak slots
will dig at them forever.

`exhausted_keys` (slots at the dig cap) move to `accepted` if they hold a value
and `skipped` if they do not. Neither appears in `open_gaps`, and `accepted`
counts toward displayed progress — the candidate answered; the engine simply
stopped demanding a number.

### Bounds

| Constant | Value | Purpose |
|---|---|---|
| `MAX_DIGS_PER_SLOT` | 2 | Questions per slot before it is settled |
| `QUESTION_MEMORY` | 12 | Questions fed back into the prompt |
| `SOFT_TURN_NUDGE` | 20 | UI starts pushing the finish option |
| `HARD_TURN_CAP` | 40 | Engine closes the interview itself |

Completion is checked **before** any open question is served. A candidate who
finishes while a question is on screen must not have it handed back.

## The LLM seam

`llm.py` is the only module that talks to a model, and it exposes one function
plus one exception. Provider selection order:

1. `CAREER_ADVISOR_LLM_PROVIDER` — `module.path:function`, if set
2. `agent_orch.llm.complete` — the default seam, if that package is installed
3. Otherwise, `LLMError` with an actionable message

The import never fails, so the app, its tests, and any static analysis work in
a clone with no provider at all. Prompts and JSON contracts live in
`interview.py` and `documents.py`, not in the seam.

## Security posture

| Concern | Handling |
|---|---|
| Invite links carry access | Exchanged for an httpOnly, `Secure`, `SameSite=Lax` cookie on first visit; the code is then dropped from the URL via redirect |
| Invite brute force | Per-IP failure cap over a sliding window (`InviteRateLimiter`) |
| Credentials | Never in environment variables; invite codes and session tokens exist only in the database |
| Token strength | 128-bit invite codes, 256-bit session tokens (`secrets.token_urlsafe`) |
| Document provenance | Generated from stored facts only, never raw transcripts |
| Irreversible actions | No auto-submit path for applications exists |

Session scoping is enforced in the queries themselves — document reads take
both an id and a session token, so one session cannot read another's artifacts.

## Testing

62 tests, no network and no model calls. The LLM is injected as a
`complete_fn` in engine tests and monkeypatched in route tests, so every
interview path is exercised deterministically with scripted model output.

Tests that exercise the default Codex seam skip cleanly when that optional
package is absent.
