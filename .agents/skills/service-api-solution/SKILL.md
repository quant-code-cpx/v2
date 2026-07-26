---
name: service-api-solution
description: Create, review, or revise service-api technical方案 and detailed designs for NestJS modules, public or internal APIs, authentication and authorization, Prisma data models, Redis security state, downstream clients, compatibility, reliability, and rollout. Use whenever work concerns a new or changed API capability under service-api or docs/service-api/.
---

# Service API Solution

Design contract-first API capabilities with explicit module ownership, security boundaries, failure behavior, and migration paths.

## Mandatory base

1. Read and follow `../technical-solution/SKILL.md` first, including its standard and HTML template.
2. Apply this skill as the service-api overlay. Resolve conflicts in this order: user request, nearest `AGENTS.md`, this skill, base skill.
3. Place the canonical proposal under `docs/service-api/<NNNN>-<topic>/`.
4. Store public/internal OpenAPI, event schemas, or shared data contracts under `docs/contracts/`.
5. Create or update an ADR for module boundaries, auth, persistence, public compatibility, deployment, or hard-to-reverse decisions.
6. When the design changes another service boundary, also load that service's solution skill and keep one named owning proposal plus linked impact/contract changes.

## Workflow

1. Read `service-api/README.md`, existing ADRs/contracts, Prisma schema, configuration, and affected code. Use CodeGraph first when indexed.
2. Establish the business contract:
   - actor, permission, use case, success result, failure result;
   - exposure mode: anonymous public, authenticated public, partner, or internal;
   - consumer, request volume, latency/freshness, consistency, and compatibility needs;
   - authoritative owner of every read or write.
3. Define module ownership and dependency direction before Controller/Service/Repository structure.
4. Design the external contract:
   - method, path, version, request/response schema, validation, status, Problem Details;
   - pagination, filtering, sorting, idempotency, concurrency, caching, and deprecation;
   - request/correlation ID, audit context, and sensitive-field handling.
5. Design security:
   - authentication, authorization, tenant/resource checks, rate limits, abuse cases;
   - token/session invalidation and `securityVersion` impact;
   - cookie, CSRF, CORS, proxy trust, secrets, and data minimization.
6. Design persistence:
   - Prisma model ownership, constraints, indexes, transaction boundary, isolation;
   - migration/deploy job, expand-contract compatibility, rollback, backfill, and recovery;
   - Redis only for short-lived security state, never authoritative users, credentials, sessions, or business data.
7. Design downstream integration:
   - versioned API/event only; never access data-sync storage directly;
   - timeout, retry safety, circuit/open state, schema validation, fallback, and 503 mapping.
8. Define performance and operations:
   - latency/error budgets, connection limits, query plan, payload bounds;
   - health/readiness, logs, metrics, traces, alerts, dashboards, and runbook entry.
9. Read `references/design-checklist.md` completely and close every applicable item.
10. Validate proposal HTML, contracts, and local links. Execute document-level checks; list migration, rollback, and implementation acceptance commands as planned/not run when no implementation exists. Do not implement unless the user also requests implementation.

## Required proposal content

Include, when applicable:

- one capability card covering actor, permission, owner, consumer, SLO, and compatibility;
- module dependency diagram and normal/auth/validation/dependency-failure flows;
- complete API contract examples plus error and idempotency behavior;
- data model, constraints, indexes, transactions, migration, backfill, and rollback;
- threat/failure analysis, observability, rollout, compatibility window, and acceptance commands.

## Repository guardrails

- Keep `AuthModule → UserModule` and `RedisModule` one-way; never use `forwardRef()`.
- Increment `securityVersion` for user disable, password change, or role change.
- Keep PostgreSQL authoritative for users, credentials, sessions, audit, and business state.
- Keep Redis limited to short-lived auth security state.
- Keep production migrations separate from application startup.
- Do not guess endpoints, schemas, auth policy, or downstream capabilities.
- Do not expose internal exceptions, secrets, credentials, or restricted fields.
- Do not let service-web bypass service-api or let service-api bypass downstream versioned boundaries.
