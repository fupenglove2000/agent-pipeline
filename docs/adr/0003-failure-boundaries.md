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

## Implementation notes (2026-10-18)

Wiring this table into `run_task` (`graph/failures.py`, `runner/single.py`)
settled several points the decision above left open. They are recorded here
because each is a behaviour a reviewer will see in the code and ask about.

**Where retries live.** Transient failures are retried by a LangGraph
`RetryPolicy` on the `call_model` node (3 attempts, exponential backoff with
jitter), counted independently of `MAX_PARSE_ATTEMPTS`. The OpenAI SDK's own
built-in retry is explicitly disabled. Left on, the two layers multiply — each
graph attempt can itself be retried several times inside the SDK — and the cap
in the table above would no longer be the real cap. The cost of owning retries
in one layer: the SDK honours `Retry-After` headers on rate-limit responses,
and our policy does not yet.

Disabling the SDK's retry also means inheriting what it used to cover. Its
retry conditions were compared against the transient category, and server
errors (5xx, `InternalServerError`) were added to it. The SDK also retried 408
and 409; both were left out on purpose. They are rare for chat completions,
and 408 has no dedicated exception class — covering it would mean matching the
base status-error class and branching on the status code, pulling the whole
base class into classification. If either occurs, it surfaces as an
unclassified failure, to be added to the table deliberately. Tests pin this
choice, so adding them later is a visible decision rather than a drive-by edit.

**What the retry predicate may see.** Which exceptions count as transient is
decided in `graph/failures.py`, not in the graph. The predicate passed to the
graph returns `False` for anything it does not recognise, so an unknown
exception is never retried; it reaches `run_task`, where classification raises
`UnclassifiedFailureError`. There is no default category: an exception outside
the table is a gap in the table.

**Outcome per category once the graph gives up:**

| Category | `task_attempt` row | `Task.status` |
|---|---|---|
| Malformed output (cap reached) | FAILED, category recorded | ABANDONED |
| Non-retryable | FAILED, category recorded | ABANDONED |
| Transient (cap reached) | FAILED, category recorded | stays RUNNING |
| Systemic | none | stays RUNNING; exception re-raised |

Two of these rows need justification:

- *Transient after the cap does not abandon.* Exhausting transient retries says
  the environment was unhealthy for a few seconds, not that the task is bad.
  Abandoning would throw away a task that will very likely succeed in a later
  run, so it stays RUNNING to be picked up again.
- *Systemic writes no attempt row.* This is a deliberate exception to
  ADR-0002's "every execution appends a `task_attempt` row". A systemic failure
  (expired credentials, unreachable database) belongs to the run, not to the
  task in hand. Recording it against the task would make a healthy task's
  history look failed, and when the database is what failed, the write may not
  succeed anyway. The run-level record of an abort belongs to the runner, which
  does not exist yet.

**Known gaps, accepted for now:**

- No exception maps to *non-retryable* yet. A provider refusal may arrive as a
  normal response (for example a content-filter finish reason) rather than as
  an exception. In that case it currently fails parsing and is retried as
  malformed output — the waste this ADR exists to prevent. To be confirmed
  against the real API.
- `OperationalError` is treated as "database unreachable", but deadlocks and
  serialisation failures share that type and are transient. Harmless while
  execution is single-process; to be narrowed when the runner adds concurrency.
- When the graph raises, its state is lost, so tokens spent by earlier
  successful model calls in that execution are recorded as 0. Checkpoint
  persistence fixes this by keeping the last saved state readable.
- `run_task` sets RUNNING without a lease, so `reclaimable_tasks()` cannot see
  tasks left RUNNING by the transient and systemic paths. Re-running the same
  input picks them up today; the lease belongs with the runner, the first
  caller of `reclaimable_tasks()`.