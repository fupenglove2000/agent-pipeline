# ADR-0001: Use LangGraph as the orchestration layer for the agent pipeline

Status: Accepted
Date: 2026-08-16

## Context

`agent-pipeline` runs an LLM agent over a batch of tasks: for each item the agent reads the input, calls tools (retrieval, computation, external APIs), and produces a structured result. The orchestration layer has to satisfy the following constraints, which come from treating this as a production batch workload rather than a demo:

- **Control flow is data-dependent.** Whether the agent needs another tool call, a retry, or human review is decided at runtime from the model's output. It is not a fixed sequence of steps.
- **Runs are long and interruptible.** A batch may take hours. If the process dies at item 4,000 of 10,000, it must resume without re-running completed work — and without re-paying for it. Resumption therefore needs **durable intermediate state**, not just a position counter.
- **Every step can fail, and differently.** Rate limits, timeouts, malformed model output, and tool errors each call for a different response (retry with backoff, re-prompt, skip and record, abort the run).
- **Cost is a first-class runtime concern.** Token spend has to be measurable per step so a budget ceiling can be enforced mid-run.
- **Behaviour must be inspectable.** When one item produces a bad result, it must be possible to see which path it took and what each step received.

## Options Considered

### 1. Hand-written loop calling the model API directly

- Pros: no framework dependency, complete control, easiest to reason about at small scale.
- Cons: every constraint above has to be built by hand — a state machine for the agent's decisions, serialisation of that state to survive restarts, per-branch retry policies, tracing. These are exactly the parts that are easy to get subtly wrong and tedious to test. In practice this path converges on a private, half-finished orchestration framework with no documentation and no community.

### 2. LangChain only (chains / AgentExecutor)

- Pros: mature tool abstractions and retrieval integrations; less ceremony for straightforward flows.
- Cons: `AgentExecutor` is essentially an opaque loop. Branching, mid-run interruption and resumption are not first-class — the agent's intermediate state lives inside the loop and is not durably persisted. Enforcing a budget ceiling or resuming from step *n* means working against the abstraction rather than with it.

### 3. LangGraph

- Pros: the agent is modelled explicitly as a **state graph** — nodes, edges, conditional edges — so the control flow is declared rather than implied. Its **checkpointer** persists graph state after each node, which gives interruption, resumption and time-travel debugging directly. Per-node boundaries are natural attachment points for retry policies, cost accounting and tracing. Interoperates with LangChain tools and retrievers, so option 2's strengths remain available.
- Cons: additional concept to learn (graph/state/checkpointer) and a real dependency on a fast-moving library. Overkill for a single-shot prompt.

### 4. General workflow engine (Temporal, Airflow, Prefect)

- Pros: strong durability and scheduling guarantees, mature operational tooling.
- Cons: solves *orchestration between* jobs, not *the agent's own decision loop*. The LLM-specific concerns — token accounting, re-prompting on malformed output, tool-call routing — would still have to be implemented inside a task. Adds a separate runtime for a system that currently has one job.

## Decision

Use **LangGraph** as the orchestration layer, with LangChain for tool and retrieval abstractions.

The deciding factor is durable intermediate state. Resumability and per-step cost control are requirements here, not nice-to-haves, and LangGraph provides them at the node boundary; options 1 and 2 would require re-implementing them. Option 4 addresses a level of the problem this project does not yet have.

## Consequences

- We accept a dependency on a fast-moving library. Mitigation: keep domain logic (tools, evaluation, budget accounting) outside the graph nodes so the graph layer stays thin and replaceable. No business rule should live in an edge condition.
- The checkpointer needs a durable backend. PostgreSQL is used for both checkpoints and run records, so there is one store to operate.
- Checkpointing makes resumption possible but does **not** make it correct. Because LLM output is non-deterministic, "the same input produces the same result" cannot be assumed, so idempotency has to be defined at the task level rather than the output level — decided separately in ADR-0002.
- Which failures are retried, skipped, or fatal is expressed as node-level policy — decided in ADR-0003.
- Node boundaries are where token cost is recorded and the budget ceiling is enforced — see ADR-0004.
- If the project later grows to several coordinated pipelines, revisit option 4 as a layer **above** LangGraph, not as a replacement for it.
