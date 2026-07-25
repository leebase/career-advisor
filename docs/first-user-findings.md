# What one real interview taught us

The MVP shipped with a passing test suite, a clean deployment, and a delivery
checklist that it fully satisfied. Then one real candidate used it.

Their feedback was two sentences: the app asked very similar or identical
questions several times, and answering it felt like talking into a void — hours
of questions with no response, just the next question. They compared it to an
interview where the interviewer never reacts to anything you say, and said it
started to feel maddening.

Both points were understated. This document is what the session data showed,
what caused it, and what changed. Details identifying the candidate have been
removed; the metrics are real.

## The session

| Measure | Value |
|---|---|
| Turns | 78 |
| Distinct facts captured | 21 (3.7 turns per fact) |
| Profile slots filled by the end | 19 of 24 |
| Questions containing "personally own" | 18 |
| Questions containing "day to day" | 15 |
| Average answer length, first 10 turns | 1,022 characters |
| Average answer length, last 10 turns | 105 characters |
| Interview ended by | nothing — they stopped replying |

Four separate times the candidate said, in their own words, that they had
already answered a question or asked whether they had given enough for a
resume. There was no code path for any of that to mean anything, so the engine
asked again each time. The final question went unanswered.

At one point the engine told them it could not access their earlier answers.
That was true, and it was a bug talking.

The engine was never short of material. Coverage was 19/24 by the end. It
simply had no way to recognise it was done.

## Five defects

### 1. The model could not see the questions it had already asked

The prompt contained the schema, the facts collected so far, the open gaps and
the candidate's latest answer. It did **not** contain the question history. An
engine that knows only its extracted facts cannot know it just asked this;
repetition was structurally unavoidable rather than a lapse in wording.

*Fix:* the last 12 questions go into every prompt with an explicit
no-repeat/no-reword rule, plus an instruction never to claim it cannot see
earlier answers.

### 2. The weak-fact heuristic could not be satisfied

Facts in scale-heavy sections were classified `weak` unless the text contained
a digit or a metric word. Weak slots stayed in the open-gaps list, and the
prompt instructed the model to prefer digging at weak slots. Nothing capped how
often one slot could be asked about.

The result was a loop with no exit. Two slots — both stored at high confidence
(0.93 and 0.78) with real evidence text, and both perfectly good answers that
happened to contain no number — consumed roughly the last third of the
interview. Confidence could only ever *downgrade* a fact's classification,
never satisfy it.

*Fix:* at most two questions per slot, tracked via a `target_key` the model
returns and the database records; settled slots leave the open-gaps list
entirely; and confidence at or above 0.75 with evidence satisfies a slot
without requiring a number.

### 3. Nothing bounded the interview

Completion was entirely the model's judgement, evaluated against open gaps that
defect 2 guaranteed would never close. There was no turn cap and no way for the
candidate to say "enough".

*Fix:* a persistent finish action, a nudge at 20 turns, a hard stop at 40. A
test written for this immediately caught that the exit was itself broken — the
engine served any dangling open question *before* checking completion, so
finishing handed the same question straight back. Completion is now checked
first.

### 4. The feedback already existed and was thrown away

This is the one worth internalising. The engine computed, on every single turn,
the facts it had extracted, their evidence, their confidence, and a short
rationale for why it was asking the next question. All of it was returned by
the engine and persisted to the database. The page rendered **none** of it.

The "void" was not a missing feature. It was a template that dropped data the
system already had. Section progress bars made it worse by counting only
`filled` slots, so a good answer that landed in `weak` moved the bar not at
all.

*Fix:* after every answer the page shows what was captured and why the next
question is being asked, and progress counts accepted slots.

### 5. Late answers silently overwrote better earlier ones

Facts are stored one row per slot, and writes were unconditional. As the
candidate tired and their answers shortened, thin late answers replaced richer
early content. The final stored value for one slot was a throwaway sentence
from turn 73 that had overwritten a detailed answer from turn 39.

*Fix:* strictly weaker writes are refused — an explicit correction always still
wins — and every extraction, kept or rejected, is appended to a history table.

## The lesson we'd want someone else to take

None of these were model failures. Every one was our code, and every one of
them passed:

- the full test suite
- a deployment health check
- a delivery checklist that asked whether the profile store contained
  evidenced structured facts — it did, 21 of them

Nothing anywhere in that bar asked **what the interview cost the human being
answering it**. A quality gate that inspects artifacts and never the experience
of producing them will keep passing products that are miserable to use. If you
are building a conversational data-collection tool, the metric that would have
caught all five defects in five minutes is simply: *read one real transcript
end to end.*

That is now a command, `career-advisor review-session`, because diagnosing this
took two rounds of hand-written SQL against a production database. It reports
reworded-duplicate question pairs, engagement decay, turns per fact, coverage,
and any line where the candidate pushed back. Run against the session described
here it found a fifth pushback line that careful manual reading had missed.

The candidate's session is now the regression fixture. Their closing note, after
78 questions, was that they'd enjoy talking about the project some time — which
is more grace than the interview earned.
