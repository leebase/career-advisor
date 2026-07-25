# Contributing

Thanks for looking at this. It's a small project with one strong opinion:
**the interview experience is the product**, so changes are judged by what a
real session looks like afterwards.

## Setup

```bash
pip install -e ".[dev]"
pytest          # 61 tests, no network, no model calls
ruff check src tests
```

No LLM provider is needed to develop or test. See the README if you want to run
a live interview.

## Ground rules

**Tests must pass, and new behaviour needs a test.** The interview engine is
tested by injecting a `complete_fn` that returns scripted JSON, so any path can
be exercised deterministically — including failure paths. Look at
`tests/test_interview.py` for the pattern.

**If you change the interview engine, measure a session.** Run
`career-advisor review-session` before and after. Three numbers matter: the
repeat rate, the engagement trend (do answers get shorter?), and turns spent
per fact captured. A change that improves fact coverage while pushing the
repeat rate up is not an improvement.

**Never regress these three properties.** Each one exists because its absence
was observed harming a real person's session — the account is in
[docs/first-user-findings.md](docs/first-user-findings.md):

1. The model can see the questions it has already asked.
2. Digging at any one slot is bounded, and the candidate can always end the
   interview.
3. Every answer produces visible feedback about what was captured.

**Don't add an auto-submit path.** Applications are never submitted without a
human. This is a product invariant, not a to-do.

**Documents come from stored facts, never raw transcripts.** If a generator
needs something, it should be a fact slot in the schema.

## Adding a domain

The profile layer is domain-agnostic. A new domain is a YAML file in
`src/career_advisor/schemas/` with the shape
`domain / version / sections → facts`, where each fact carries a
`description`, the `evidence` needed to consider it real, and a `priority` of
`required` / `preferred` / `optional`. The engine derives its questions from
slot coverage, so a good schema is most of a good interview.

Note that `classify_fact` treats a few section ids as scale-heavy for the
purpose of demanding metrics. If you add a domain where that heuristic doesn't
fit, make it schema-driven rather than adding another hardcoded section name.

## Style

- `ruff` and `black` config live in `pyproject.toml`; line length 88.
- Comments should explain *why*, especially where a value was chosen because
  something went wrong without it. Several constants in `interview.py` look
  arbitrary and are not — keep the reasoning attached.
- Docstrings on modules and non-obvious functions; skip them on the obvious.

## Reporting problems with an interview

The most valuable bug report here is a transcript. If an interview went badly,
`career-advisor review-session --transcript` output (redact as needed) says
more than a description will. The first user's feedback was two sentences long
and led to five real defects.
