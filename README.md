# Career Advisor

An interview-driven career profile builder. It talks to a candidate one
question at a time, extracts **structured facts with evidence** into a schema,
and generates a resume and job-search strategy from those facts — never from
raw chat transcripts.

The interesting part is not the resume generation. It's the interview: an
engine that digs for specifics, knows what it has already asked, knows when to
stop, and shows the candidate what each answer actually bought them.

> Status: working MVP, MIT licensed. Runs a real interview end to end. Built
> and hardened against one real candidate session — see
> [docs/first-user-findings.md](docs/first-user-findings.md), which is the most
> useful document in this repo if you are building anything similar.

## What it does

- **Invite-gated sessions.** An invite link is exchanged for an httpOnly
  session cookie on first visit, then dropped from the URL. Rate-limited,
  revocable. Interviews resume where they left off if the browser closes.
- **Schema-driven interview.** A domain schema (`schemas/candidate.yaml`)
  defines fact slots with descriptions, required evidence and priority. The
  engine works from slot coverage, not a fixed question list, so it adapts to
  what the candidate has already said.
- **Evidenced facts.** Every turn returns validated JSON: extracted facts with
  evidence, a confidence score, and one follow-up question. Facts are stored
  per slot with full history.
- **Documents from facts.** Versioned resume and job-search strategy generated
  from the stored profile, with prior versions kept.
- **Interview health as a command.** `review-session` reads a real interview
  back and reports repeated questions, engagement decay, turns spent per fact,
  and anywhere the candidate pushed back.

The profile layer is domain-agnostic: any YAML file with the shape
`domain / version / sections → facts` works, so "candidate" is a
specialization rather than something baked into the engine.

## Quick start

```bash
git clone https://github.com/leebase/career-advisor.git
cd career-advisor
pip install -e ".[dev]"
pytest                      # 61 tests, no model calls, no network

career-advisor serve        # http://127.0.0.1:8611/career-advisor/
```

The app needs an LLM provider before it can run an actual interview — see
below. Everything else, including the whole test suite, works without one.

Mint an invite to reach the app (there is no open sign-up by design):

```bash
career-advisor create-invite "Name" --base-url http://127.0.0.1:8611/career-advisor/
```

### Configuring an LLM provider

The engines depend on exactly one function: `complete(prompt) -> str`. Point
`CAREER_ADVISOR_LLM_PROVIDER` at any callable with that shape:

```bash
export CAREER_ADVISOR_LLM_PROVIDER="myproject.provider:complete"
```

```python
# myproject/provider.py
def complete(prompt: str) -> str:
    """Return the model's reply as text. Raise on failure."""
    ...
```

By default the app looks for `agent_orch.llm` (a Codex CLI seam from the
project this was built inside, pinned to `gpt-5.6-terra` at medium effort).
That package is not required and is not on PyPI — if it is missing, the import
still succeeds and only real model calls fail, with a message telling you
what to do. Nothing else in the codebase knows or cares which model answers.

Other environment knobs, all optional: `CAREER_ADVISOR_DB` (database path),
`CAREER_ADVISOR_BASE_URL` (what invite links point at),
`CAREER_ADVISOR_FOOTER` (the footer line on every page).

### Docker

```bash
docker compose up --build -d
curl -s http://127.0.0.1:8611/career-advisor/health
```

That works from a fresh clone with nothing else installed. The image contains
the app and no provider, so give it one the same way — by setting
`CAREER_ADVISOR_LLM_PROVIDER` in the service environment, along with whatever
your provider needs mounted or installed.

Put anything specific to your deployment in `docker-compose.override.yml`,
which Compose merges automatically and this repository ignores:

```yaml
services:
  career-advisor:
    environment:
      CAREER_ADVISOR_LLM_PROVIDER: "myproject.provider:complete"
      CAREER_ADVISOR_BASE_URL: "https://example.com/career-advisor/"
      CAREER_ADVISOR_FOOTER: "Run by Example Corp."
```

The `Dockerfile` also has a `codex-seam` target that installs the default
`agent_orch.llm` provider from a named build context, if you happen to have
that package.

`deploy/webroot-default.conf` is an nginx drop-in for serving the app under a
`/career-advisor/` path prefix behind a shared static webroot. Every route is
prefixed by the app itself, so direct and proxied access behave identically.

## Reviewing a real interview

The health of the interview is the whole product, so reading one back is a
command rather than an afternoon with SQLite:

```bash
career-advisor review-session --list
career-advisor review-session [TOKEN] [--transcript]
```

```
Session 4kQx8vTn2p…  (Example Candidate)
  turns: 78 (70 answered, 8 skipped)
  facts: 21  —  3.7 turns per fact
  ended: still open

  REPEATED QUESTIONS: 17 pairs (22% of turns)
    Q31 ≈ Q44 (0.78)
      What systems did you personally own day to day, and how many users?
      Which systems were you responsible for day to day versus assisting with?

  ENGAGEMENT: first 10 answers avg 1022 chars, last 10 avg 105 chars
    ⚠ answers shrank by more than half — candidate disengaged

  ⚠ CANDIDATE PUSHED BACK:
    Q75: (candidate says they have already answered this)
```

Repeat detection is by keyword overlap, not string equality, because the
failure mode is *reworded* duplicates. A high repeat rate, shrinking answers,
or any pushback line is a defect in the engine, not a quirk of that candidate.

## How the interview engine works

Each turn is a single model call returning validated JSON — extracted facts,
one follow-up question, the slot that question targets, and a short rationale
shown to the candidate. State lives in SQLite, so a session survives a closed
browser.

Three rules keep it from becoming an interrogation, each of which exists
because its absence was observed doing real damage:

1. **It sees its own history.** The last 12 questions go into every prompt with
   an explicit no-repeat/no-reword rule. Without this, an engine that only
   knows its *extracted facts* will re-ask a question indefinitely whenever an
   answer fails to close a slot.
2. **Digging is capped.** At most two questions per slot; then the best
   available answer is accepted and the slot is settled. High confidence with
   real evidence can satisfy a slot even with no number in it.
3. **The candidate can end it.** A persistent finish action, a nudge at 20
   turns, a hard stop at 40. "I've given you enough" is a supported input, not
   a sentence the engine reads past.

And one rule about feedback: after every answer the page shows the facts that
were captured and why the next question is being asked. An interview that
replies only with another question feels like shouting into a void, however
good the questions are.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the component map and data model.

## Design boundaries

- **Applications are never auto-submitted.** No code path exists for it.
- **Documents are generated from stored facts only**, never from raw
  transcripts, so what goes into a resume is inspectable.
- **No credentials in environment variables.** Invite codes and session tokens
  live only in the database; `CAREER_ADVISOR_DB` holds a path, not a secret.
- **No job-board scraping.** Opportunity discovery, when it lands, uses APIs
  and feeds.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: `pytest` must pass,
and if you change the interview engine, say what a real session looks like
afterwards — `review-session` is how that gets measured.

## License

MIT — see [LICENSE](LICENSE).
