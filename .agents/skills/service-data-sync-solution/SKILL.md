---
name: service-data-sync-solution
description: Create, review, or revise service-data-sync technical方案 and detailed designs for synchronization tasks, provider adapters, canonical datasets, persistence, data quality, scheduling, backfills, recovery, observability, and internal data contracts. Use whenever work concerns a new or changed sync capability under service-data-sync or docs/service-data-sync/.
---

# Service Data Sync Solution

Design synchronization capabilities that remain provider-neutral, repeatable, recoverable, and operationally diagnosable.

## Mandatory base

1. Read and follow `../technical-solution/SKILL.md` first, including its standard and HTML template.
2. Apply this skill as the service-data-sync overlay. Resolve conflicts in this order: user request, nearest `AGENTS.md`, this skill, base skill.
3. Place the canonical proposal under `docs/service-data-sync/<NNNN>-<topic>/`.
4. Create or update an ADR for cross-service, storage, provider-routing, deployment, or hard-to-reverse decisions. Record its status explicitly; an ADR may compare open options, but it must not silently turn an unresolved source or platform choice into an accepted decision.
5. Keep machine-readable cross-service contracts under `docs/contracts/`; link them from the proposal.
6. When the design changes another service boundary, also load that service's solution skill and keep one named owning proposal plus linked impact/contract changes.

## Workflow

1. Read `service-data-sync/README.md`, current configuration, relevant research documents, and nearby code. Use CodeGraph before code reads when indexed.
2. Establish the task contract:
   - business purpose and downstream consumer;
   - dataset and canonical capability;
   - market, timezone, calendar, granularity, date range, and freshness;
   - expected volume, historical depth, correction behavior, and source entitlement.
   - when market data is involved, adjustment basis, corporate-action effects, suspension/no-trade semantics, units, and revision policy.
   - when a metric is vendor-derived, its semantic family, methodology owner/version, universe, bucket definition, denominator, window, cutoff, and finalization state.
3. Separate facts, assumptions, decisions, and unresolved questions. Do not promote provider research into production approval.
4. Classify each constraint before making it a gate:
   - use a hard gate only when proceeding would make the design technically infeasible, unsafe, non-compliant, non-recoverable, or incapable of meeting its stated data contract;
   - give every hard gate a measurable entry condition, evidence, verifier, and exit condition;
   - record approvals, ownership assignment, budgets, scheduling, stakeholder availability, and other non-technical dependencies as risks or follow-up actions, not execution blockers; give them an owner, a due point, and a technically safe fallback where possible;
   - do not invent prerequisite work or turn an unresolved non-technical question into a stop condition.
5. Design provider isolation:
   - keep SDKs, URLs, and vendor fields inside one adapter;
   - emit provider-neutral batches through existing ports;
   - prevent tasks, application, quality, and persistence code from importing concrete providers;
   - keep adapters unable to write the canonical database directly.
   - preserve upstream source and methodology as canonical provenance; provider-neutral does not mean methodology-neutral.
6. Design execution semantics:
   - trigger, schedule, partitions, full/incremental mode, checkpoints, backfill;
   - idempotency key, concurrency lock, retry budget, timeout, cancellation, resume, and rerun;
   - success, partial success, quarantine, and terminal failure states.
7. Design the data lifecycle:
   - source payload boundary, canonical schema, provenance, observed/effective time, version;
   - validation, deduplication, completeness, reconciliation, revision, and quality gates;
   - PostgreSQL/S3 ownership, transaction boundary, migration, rollback, retention, and cleanup.
8. Design runtime operations:
   - rate limits, credentials, egress, resource limits, health/readiness;
   - run/batch correlation, structured logs, metrics, traces, alerts, diagnostics, and operator recovery.
9. Design consumer access through versioned API or event contracts. Never grant service-api or service-web direct database/provider access.
10. For vendor-derived or semantically ambiguous market data such as fund flow, main-force flow, order-size buckets, sentiment, or estimated positions, read `references/derived-market-data.md` completely and apply its comparability gate.
11. Read `references/design-checklist.md` completely and close every applicable item before finalizing.
12. Validate the HTML proposal, links, and Compose impact. Execute document-level checks; list implementation acceptance commands as planned/not run when no implementation exists. Do not implement unless the user also requests implementation.

## Required proposal content

Include, when applicable:

- one task-definition card covering source, market semantics, partition, cadence, and consumer;
- normal, duplicate, provider-failure, schema-drift, persistence-failure, and recovery flows;
- canonical data model plus provenance and temporal semantics;
- idempotency, checkpoint, backfill, correction, retention, and rollback strategies;
- quality rules with measurable thresholds and disposition;
- a gate register that separates measurable technical gates from non-technical risks and follow-up actions;
- deployment topology, migration jobs, secrets, egress, and operational ownership;
- acceptance commands using the repository Docker-only workflow.

## Non-negotiable boundaries

- Do not invent an undecided source, scheduler, transport, SLA, or schema.
- Do not add approval, budget, staffing, scheduling, ownership, or stakeholder-response items as hard execution gates. Track them separately with an owner, timing, and fallback; preserve hard gates for technical correctness, security, compliance, data integrity, recoverability, and acceptance evidence.
- Do not call vendor SDKs outside adapters.
- Do not let adapters write canonical storage.
- Do not canonicalize a vendor label as a universal market fact or silently substitute a differently defined source.
- Do not mix reported values, vendor estimates, intraday snapshots, daily facts, or rolling-window snapshots in one arithmetic series without an explicit, versioned transformation.
- Do not define missing dates as market closure without authoritative evidence.
- Do not omit timezone, trading-calendar, idempotency, recovery, migration, or rollback design.
- Do not treat Redis as authoritative dataset storage.
- Do not use host Python/uv for service validation; use the documented Docker workflow.
