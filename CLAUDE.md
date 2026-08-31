# CLAUDE.md

## What this project is

`agent-pipeline` runs LLM agents over batch workloads in a way that survives production:
LangGraph checkpoints, retry policies, cost ceilings and output evaluation.

This is a portfolio project for a backend engineer with 19 years of experience
(large-scale enterprise batch systems, data pipelines) moving into AI agent
development. It is judged by hiring engineers who will open the repo and ask
"why did you do it this way?" — so **design reasoning matters more than feature count**.

## Non-negotiable rules for you (the coding agent)

1. **Never write or edit anything under `docs/adr/`.** Those are the author's own
   design documents. If a task seems to need a new ADR, say so and stop.
2. **Work in small increments.** One coherent change at a time. After each one,
   stop and report what you did and why, then wait. Do not chain multiple
   features in a single response.
3. **Explain before you implement.** For anything beyond a trivial edit, state the
   approach and the trade-off in 3–5 lines first, then write the code.
4. **No new dependencies without asking.** If a library seems necessary, explain
   why the standard library or an existing dependency is not enough, and wait.
5. **No speculative abstractions.** No base classes, plugin registries, or
   config layers for cases that do not exist yet. Concrete and readable beats
   general.
6. **Every behavioural rule gets a test.** Retry limits, budget ceilings, state
   transitions, key derivation — if it is a rule, it has a test.
7. **Keep it explainable.** Prefer the version a reviewer can follow over the
   clever one. If a piece of code needs a comment to be understood, consider
   whether simpler code would not.
8. **Do not commit or push.** The author reviews and commits.

## Design decisions already made (read these first)

- `docs/adr/0001-use-langgraph.md` — LangGraph is the orchestration layer;
  the deciding factor was durable intermediate state. Keep domain logic
  (tools, evaluation, budget accounting) **outside** graph nodes so the graph
  layer stays thin and replaceable. No business rule in an edge condition.
- `docs/adr/0002-task-level-idempotency.md` — idempotency is a property of the
  task, not the output. `task_key = hash(source_id, normalised_input)`.
  Terminal states are `SUCCEEDED` and `ABANDONED` only. Every execution appends
  a `task_attempt` row; retries never overwrite history. Task state is
  authoritative, checkpoints are an optimisation.

Decisions still open (do not invent answers, ask instead):
- Failure boundaries: which errors retry, which abandon, which abort the run (ADR-0003)
- Cost control strategy (ADR-0004)
- Output evaluation / acceptance criteria (ADR-0005)

## Stack

Python 3.12, uv, LangGraph + LangChain, FastAPI, PostgreSQL (SQLAlchemy 2.x,
psycopg 3, Alembic), pytest + Testcontainers, ruff, mypy strict, structlog.

## Layout

```
src/agent_pipeline/
  graph/    LangGraph state graph: state, nodes, edges
  tools/    tools the agent can call
  runner/   batch submission, concurrency, resume
  budget/   cost accounting and ceilings
  eval/     output evaluation
  api/      FastAPI
tests/
docs/adr/   design decisions — yours to read, never to write
```

## Conventions

- Conventional Commits (`feat:` `fix:` `test:` `ci:` `docs:` `refactor:`)
- `ruff check`, `ruff format`, `mypy` (strict) and `pytest` must all pass
- Type hints everywhere; `Any` needs a reason
- LLM calls are mocked in tests — the suite runs offline and costs nothing
