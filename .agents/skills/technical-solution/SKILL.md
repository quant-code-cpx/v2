---
name: technical-solution
description: Create, review, or revise repository technical方案、技术设计、架构方案 and implementation proposals as concise, polished, self-contained HTML documents. Use when work asks for a technical proposal, solution design, architecture design, module design, API or data design, implementation plan, technology comparison, rollout design, or updates to an existing document under docs/.
---

# Technical Solution

Create reviewable technical proposals that make scope, responsibilities, flows, tradeoffs, risks, and verification explicit.

## Workflow

1. Read `references/standard.md` completely before drafting or editing a proposal.
2. Read repository guidance, relevant existing documents, and affected implementation context. Use CodeGraph first when the repository is indexed and code understanding is needed.
3. Classify proposal scope and choose its canonical directory:
   - Service-specific: `docs/service-<name>/<kebab-case-topic>/`
   - Cross-service architecture: `docs/architecture/<kebab-case-topic>/`
   - API, event, or data contract: `docs/contracts/<kebab-case-topic>/`
4. For cross-service, deployment, contract, or hard-to-reverse decisions, create or update an ADR before finalizing the proposal.
5. Use `assets/index.template.html` as structural and visual starting point. Produce `index.html` as canonical document. Remove irrelevant template sections instead of filling them with noise.
6. Keep machine-readable contracts such as OpenAPI, AsyncAPI, JSON Schema, SQL, or Proto in separate files and link them from `index.html`.
7. Validate content, local links, responsive layout, print layout, overflow, and accessibility. Render at desktop and mobile sizes when browser tooling is available.
8. Report output path, decision status, unresolved questions, and validation performed.

## Editing Existing Proposals

- Update existing canonical directory; do not create `v2`, `final`, `new`, or date-suffixed copies.
- Preserve valid decisions and working links.
- Update status, last-modified date, and change summary.
- Move superseded reasoning to ADR history instead of duplicating it throughout proposal.

## Boundaries

- Do not invent undecided technology choices. Mark assumptions and open questions explicitly.
- Do not duplicate identical content between HTML and Markdown.
- Do not embed secrets, production credentials, private endpoints, or real account data.
- Do not initialize application frameworks or dependencies unless user separately requests implementation.
