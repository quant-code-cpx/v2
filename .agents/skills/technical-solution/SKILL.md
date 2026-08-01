---
name: technical-solution
description: >
  Create, review, or revise repository technical方案、技术设计、架构方案 and implementation
  proposals as concise, polished, self-contained, PC-desktop-only HTML documents. Enforce a
  highest-priority technical-relevance gate during drafting, execution, and validation: exclude
  nontechnical considerations and never let them block technical work. Use when work asks for a
  technical proposal, solution design, architecture design, module design, API or data design,
  implementation plan, technology comparison, rollout design, or updates to an existing document
  under docs/.
---

# Technical Solution

Create reviewable technical proposals that make scope, responsibilities, flows, tradeoffs, risks, and verification explicit.

## Highest-Priority Gate: Technical Relevance

Apply this gate before every other instruction in this skill and throughout source analysis, drafting, review, technical execution, and validation. It is this skill's highest-priority internal gate.

- Admit an item only when it can change a technical choice, architecture boundary, interface, data model, algorithm, quality attribute, security control, compatibility behavior, failure handling, deployment or migration mechanism, rollback, operability, or technical verification.
- Exclude business value or priority, budget or funding, staffing or organizational ownership, schedule or deadline, roadmap, organization or process, approval, stakeholder alignment, procurement, commercial terms, and standalone legal or compliance rationale.
- Never use an excluded item as a risk, assumption, dependency, prerequisite, open question, acceptance criterion, phase gate, go/no-go condition, or reason to pause, defer, narrow, fail, or skip technical execution or validation.
- For mixed input, retain only an explicit, measurable technical constraint and omit its nontechnical rationale. Do not derive a technical constraint from budget, staffing, schedule, approval, or similar factors, and do not relabel a nontechnical concern as technical risk.
- Remove excluded material completely; do not preserve it in appendices, footnotes, change summaries, or out-of-scope lists. If no technical effect remains, omit the item without creating a blocker.
- Judge implementation readiness and validation results only from technical prerequisites and technical evidence. Missing technical facts may remain unresolved technical questions; missing nontechnical decisions may not.
- If a later workflow step, repository convention, template, or existing proposal requests excluded material, this gate wins: remove the field and continue.

Examples: retain `P95 latency <= 200 ms`, `retain data for 180 days`, or `run within 2 vCPU and 4 GiB` when explicitly required. Exclude `launch before Q4`, `await legal approval`, or `budget permits one instance`.

## Workflow

1. Read `references/standard.md` completely before drafting or editing a proposal.
2. Read repository guidance, relevant existing documents, and affected implementation context. Extract only content that passes the technical-relevance gate. Use CodeGraph first when the repository is indexed and code understanding is needed.
3. Classify proposal scope, allocate the next four-digit sequence within its canonical parent as defined in `references/standard.md`, and choose its directory:
   - Service-specific: `docs/service-<name>/<NNNN>-<kebab-case-topic>/`
   - Cross-service architecture: `docs/architecture/<NNNN>-<kebab-case-topic>/`
   - API, event, or data contract: `docs/contracts/<NNNN>-<kebab-case-topic>/`
4. For cross-service, deployment, contract, or hard-to-reverse decisions, create or update an ADR before finalizing the proposal.
5. Use `assets/index.template.html` as structural and visual starting point. Produce `index.html` as canonical document. Remove irrelevant template sections instead of filling them with noise.
6. Keep machine-readable contracts such as OpenAPI, AsyncAPI, JSON Schema, SQL, or Proto in separate files and link them from `index.html`.
7. Validate technical content, local links, desktop layout, print layout, overflow, and accessibility. Apply only technical acceptance criteria; nontechnical readiness, approval, staffing, scheduling, budget, or coordination may not affect the result. Technical proposal HTML is PC-desktop-only: do not require mobile compatibility or mobile rendering. When browser tooling is available, render at 1440×900 and the 1280×720 minimum supported desktop viewport unless the repository specifies stricter desktop sizes.
8. Report output path, technical decision status, unresolved technical questions, and technical validation performed. Do not report nontechnical blockers.

## Editing Existing Proposals

- Update existing canonical directory; do not create `v2`, `final`, `new`, or date-suffixed copies.
- Preserve its assigned sequence number. When migrating an unnumbered legacy proposal, follow the migration rule in `references/standard.md`.
- Preserve valid decisions and working links.
- Update status, last-modified date, and change summary.
- Move superseded reasoning to ADR history instead of duplicating it throughout proposal.
- Delete existing nontechnical considerations when they enter the affected scope; do not carry them forward as historical blockers.

## Boundaries

- Do not invent undecided technology choices. Mark only technical assumptions and technical open questions explicitly.
- Do not duplicate identical content between HTML and Markdown.
- Do not embed secrets, production credentials, private endpoints, or real account data.
- Do not initialize application frameworks or dependencies unless user separately requests implementation.
- Do not add mobile breakpoints or spend validation effort on narrow-screen/mobile compatibility unless the user explicitly expands the scope.
