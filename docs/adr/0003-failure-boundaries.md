# ADR-0003: Failure boundaries — retry, abandon, abort

Status: Accepted
Date: 2026-09-06

## Context

ADR-0001 established that every step can fail, and differently: rate limits,
timeouts, malformed model output, and tool errors each call for a different
response. ADR-0002 established the vocabulary for task-level state — a task
reaches a terminal state of `SUCCEEDED` or `ABANDONED`, and every execution
appends a `task_attempt` row. What is still missing is the rule that decides,
for a given failure, which of three outcomes applies:

- **retry** — try the same task again, because the failure looks transient
  or recoverable
- **abandon** — stop working this one task and mark it `ABANDONED`, but keep
  processing the rest of the run
- **abort** — stop the entire run, because continuing would be pointless or
  actively harmful

Getting this classification wrong is expensive in both directions. Retrying
a failure that will never succeed (a content-policy refusal, a permanently
malformed prompt) burns tokens and time for a result that is already known.
Abandoning a failure that was actually transient (a single rate-limit
response) throws away otherwise-good work and inflates the abandoned-task
count for no reason. And treating a systemic failure (expired API
credentials, database unreachable, cost ceiling breached) as a per-task
abandon means the run silently burns through every remaining task making the
same doomed call, instead of stopping once.

In the batch systems I have worked on, this is the job of a skip/retry
policy: an explicit table mapping exception types to an action (retry N
times with backoff, skip and record, or fail the whole job), configured once
and applied uniformly. That structure transfers directly here — what changes
is the catalogue of failure types, because LLM-specific failures (malformed
JSON, safety refusals, non-deterministic partial outputs) don't have a clean
equivalent in a typical batch job's exception hierarchy. A `NullPointerException`
from bad input data and a model deciding to refuse a request are different
enough in cause and recovery that they can't share one row of the table.

## Options Considered

### 1. Retry everything a fixed number of times, uniformly

- Pros: trivial to implement, one code path, nothing to classify.
- Cons: wastes budget retrying failures that can never succeed (a
  content-policy refusal doesn't change on the second attempt), and treats a
  database outage the same as a single flaky API call — so a systemic outage
  gets rediscovered independently by every task in the run instead of
  stopping it once.

### 2. Fixed classification table: failure category → action

A small, explicit table (in code, not configuration) mapping failure
categories to one of the three actions, each with its own retry cap where
retry applies:

| Category | Examples | Action | Cap |
|---|---|---|---|
| Transient / rate-limited | 429, connection reset, read timeout | retry with backoff | 3 attempts |
| Malformed output | JSON that fails schema validation | retry (re-prompt) | 3 attempts (already implemented as `MAX_PARSE_ATTEMPTS` in ADR-0001's graph) |
| Non-retryable model response | content-policy refusal, safety block | abandon immediately | 0 |
| Systemic | auth failure, DB unreachable, cost ceiling breached | abort run | — |

- Pros: mirrors the skip/retry policy pattern from batch systems — a
  reviewer can read one table and know the whole failure-handling behaviour
  without tracing exception flow through the code. Each category's cap is
  independent, so a rate-limit storm and a run of malformed JSON don't share
  a budget and starve each other.
- Cons: the table has to be kept honest — a new failure mode that doesn't
  fit an existing row needs a conscious decision, not a default. That is a
  process cost, not a code cost, and it's the same cost a skip/retry policy
  table carries in any batch framework.

### 3. Fully pluggable retry-policy framework (strategy objects, configurable backoff curves per category, runtime-swappable policies)

- Pros: maximum flexibility — an operator could reconfigure failure handling
  without a code change.
- Cons: this is exactly the "config layer for cases that do not exist yet"
  CLAUDE.md rules out. There is one deployment, one operator, and no
  evidence yet that the four categories above need independent tuning at
  runtime. Building the plugin surface now is solving a flexibility problem
  the project doesn't have.

## Decision

Adopt **option 2**: a fixed, in-code table classifying failures into four
categories, each mapped to retry / abandon / abort, with an independent
attempt cap per retryable category.

Two things follow from this that are worth stating explicitly:

- **Retry and abandon are per-task; abort is per-run.** A task that abandons
  does not stop the batch — it becomes a `task_attempt` row with a failure
  outcome and the run moves to the next task. An abort stops the run
  entirely and requires operator intervention before resuming, because the
  failure category (auth, DB, budget) implies every subsequent task would
  fail identically or the run has already exceeded its mandate to keep
  spending.
- **The retry cap is per category, not shared.** A task that hits three
  rate-limit errors and then succeeds is not "one attempt away from" a task
  that has already used up three malformed-output retries — they are
  independent budgets, because the categories represent different failure
  mechanisms with no reason to interact.

The re-prompt behaviour for malformed output that the current graph
implements (retry `call_model` with the same prompt, up to
`MAX_PARSE_ATTEMPTS`) is a known simplification: it does not yet feed the
validation error back into the next prompt, so it relies on the model's own
non-determinism to produce a different result rather than steering it
there. That is an accepted gap for now, not an oversight — closing it
(re-prompting *with* the error) is future work, not a blocker for this ADR.

## Consequences

- The graph and runner need a place to look up "what category is this
  exception" — a small function, not a config file, given there is exactly
  one deployment right now.
- `task_attempt` rows (ADR-0002, implemented in Week 2) must record which
  category a failure fell into, not just that it failed — otherwise the
  classification table's decisions aren't auditable after the fact.
- Abort is not yet wired to anything at this stage of the project (there is
  no multi-task runner yet — that's Week 3). This ADR defines the boundary
  now so the runner can be built against a decided policy instead of
  inventing one under time pressure later.
- The categories above are not exhaustive of every exception Python or the
  OpenAI SDK can raise — they are exhaustive of what has been observed to
  matter so far. A failure that doesn't fit any row is a bug to fix in this
  table, not a case to force into the nearest category.
