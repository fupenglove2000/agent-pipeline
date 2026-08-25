# ADR-0002: Define idempotency at the task level, not the output level

Status: Accepted
Date: 2026-08-25

## Context

A run over a batch of tasks can be interrupted and resumed (see ADR-0001). Resumption must not repeat work that has already been done — both because the side effects would be duplicated and because every repeated LLM call costs money. So the pipeline needs a definition of "this task is already done" that it can check before spending anything.

In the batch systems I have worked on, this is usually settled by a business key plus a state machine: the same key entering the pipeline twice produces the same row, and a row that has reached `COMPLETED` is skipped. Correctness there is checkable in a strong sense — reprocessing the same input yields the same output, so a duplicate run is provably harmless.

That property does not hold here. An LLM call with identical input can return a different result on the next invocation: different wording, a different tool chosen, occasionally a different conclusion. Even at temperature 0 the guarantee is best-effort — provider-side model updates, batching and quantisation all move the output. The consequences:

- Idempotency **cannot** be defined as "re-running produces the same output", because it does not.
- "Did this task already succeed?" cannot be answered by recomputing and comparing. Recomputing is exactly the cost we are trying to avoid, and the comparison would fail on noise even when both results are acceptable.
- Retries within a task (see ADR-0003) mean a single task may legitimately produce several intermediate results before one is accepted. The pipeline needs to know which one is authoritative.

Additionally, some tools the agent calls have external side effects (writing to a store, calling a third-party API). Replaying a node blindly on resume would re-trigger those.

## Options Considered

### 1. Output-hash idempotency (hash the result, dedupe on it)

- Pros: no extra state; familiar from data pipelines where transformations are pure.
- Cons: assumes determinism, which is the one thing that does not hold. Two acceptable answers to the same task hash differently, so this detects nothing useful — it would treat normal variation as a new result and identical failures as duplicates.

### 2. Semantic deduplication (embed outputs, treat near-duplicates as the same)

- Pros: tolerant of wording differences; sometimes useful for reporting.
- Cons: introduces a similarity threshold that has to be tuned per task type, and gets it wrong in both directions — two answers that differ only in phrasing may fall below the threshold, while two answers that differ in the one fact that matters may sit above it. Adds embedding cost to a mechanism whose purpose is to avoid cost. Wrong tool for a correctness decision.

### 3. Task-level completion marker with a result snapshot

Each unit of work gets a stable `task_key` derived from its input (source identifier + content hash). A `task` row carries a status (`PENDING` / `RUNNING` / `SUCCEEDED` / `FAILED` / `ABANDONED`) and, once accepted, a snapshot of the authoritative result plus the `attempt_id` that produced it. Resumption skips any task in a terminal state; a task found in `RUNNING` from a dead process is reclaimed by lease expiry and retried from its last checkpoint.

- Pros: the question "is this done?" is answered by a cheap state lookup, not by recomputation. Non-determinism becomes irrelevant to the decision. Every attempt stays recorded for evaluation and cost accounting, while exactly one is marked authoritative.
- Cons: requires durable task state alongside the LangGraph checkpoints, and requires deciding what makes a result *acceptable* — that decision moves into the evaluation layer rather than disappearing.

## Decision

Use **option 3**: idempotency is a property of the task, not of the output.

Concretely:

- `task_key = hash(source_id, normalised_input)` — stable across runs, so the same input always maps to the same task row.
- A task in `SUCCEEDED` or `ABANDONED` is never re-executed. Only these two are terminal.
- Each execution writes a `task_attempt` row (tokens, cost, latency, outcome). Retries add attempts; they do not overwrite history.
- When an attempt is accepted, its output is snapshotted onto the task and its `attempt_id` recorded as authoritative. Later attempts cannot silently replace it.
- Tools with external side effects take an idempotency key derived from `(task_key, node_name)`, so a replayed node does not duplicate the effect. Tools that cannot support this are marked non-replayable, and their node is a checkpoint boundary.

The borrowed part from conventional batch design is the business key and the state machine. The part that does not transfer is the assumption that repeated execution is verifiable by comparing outputs — so "acceptable" is decided once, by the evaluation layer, and then frozen as a snapshot.

## Consequences

- Two stores of state now exist: LangGraph checkpoints (how far *within* a task the graph got) and the task table (whether the task as a whole is done). They must not disagree. The task state is authoritative; checkpoints are an optimisation that lets a retry resume mid-task instead of restarting it.
- Input normalisation becomes load-bearing. If normalisation is unstable, `task_key` changes and completed work is silently redone. Normalisation rules need their own tests.
- Marking an attempt authoritative requires an acceptance criterion — deferred to ADR-0005 (evaluation). Until that exists, the criterion is "the attempt completed without error and produced parseable structured output".
- Lease-based reclaim of `RUNNING` tasks introduces a timeout that is a guess about how long a task may legitimately take. Set it generously; a task wrongly reclaimed costs a duplicate execution, and the attempt history will show it.
- Re-running a batch after a prompt or model change is intentionally *not* covered by this: the input is unchanged, so `task_key` is unchanged, so nothing re-runs. Forcing re-execution is an explicit operation (a new run with `force=true`), not something that happens by accident.
