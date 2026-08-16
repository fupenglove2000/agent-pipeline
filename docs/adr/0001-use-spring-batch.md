# ADR-0001: Use Spring Batch for the nightly processing job

Status: Accepted
Date: 2026-08-16

## Context

`batch-service` needs to process a large volume of business records (orders / contracts) in a nightly window. Requirements that drive the choice of framework:

- Volume is large enough that records must be processed in chunks, not loaded into memory at once.
- A run may fail halfway (DB outage, bad input rows, process killed). It must be possible to **restart from where it stopped** rather than reprocessing everything.
- Some bad rows should be **skipped and reported**, not abort the whole run.
- Every run needs an audit trail: when it started, how many items were read / written / skipped, and why it ended.
- The service runs on Spring Boot 3, and the team's main stack is Java.

## Options Considered

### 1. Hand-written loop with `@Scheduled` + JDBC

- Pros: minimal dependencies, fully transparent, quick to start.
- Cons: chunking, restart bookkeeping, skip/retry policies and run metadata all have to be built and tested by hand. Each of these is easy to get subtly wrong (e.g. restart offset, transaction boundaries per chunk). Every project that starts this way tends to grow its own half-finished batch framework.

### 2. Spring Batch

- Pros: `Job` / `Step` / chunk-oriented processing, `JobRepository` for run metadata and restartability, built-in `skip` / `retry` / `noRollback` policies, first-class Spring Boot integration and testing support (`JobLauncherTestUtils`).
- Cons: real learning curve and some ceremony; the metadata tables add schema to manage; overkill for trivial one-off scripts.

### 3. External workflow engine (Airflow, Temporal)

- Pros: strong scheduling and orchestration, good UI.
- Cons: introduces a separate runtime and operational surface for what is, at this stage, a single job. Orchestration is not the problem being solved here — reliable in-process chunk execution is.

## Decision

Use **Spring Batch** for `batch-service`.

The requirements (chunking, restart, skip/retry, run metadata) map directly onto what Spring Batch provides out of the box, and building them by hand would mean re-implementing the framework badly. Orchestration engines solve a different problem and can be layered on later if multiple jobs need coordinating.

## Consequences

- We accept the Spring Batch learning curve and its metadata tables (`BATCH_JOB_INSTANCE`, `BATCH_STEP_EXECUTION`, …) as part of the schema. Migrations must include them.
- Idempotency is **not** provided by Spring Batch itself; how re-runs of the same business batch avoid duplicate side effects is decided separately in ADR-0002.
- Which errors are skipped vs. retried vs. fatal is decided in ADR-0003, expressed through Spring Batch's skip/retry policies.
- If the project grows to multiple interdependent jobs, revisit option 3 for orchestration on top of — not instead of — Spring Batch.
