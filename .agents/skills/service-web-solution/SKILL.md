---
name: service-web-solution
description: Create, review, or revise service-web business flows, page architecture, React technical方案, routing, remote-state and API integration designs, responsive behavior, accessibility, and rollout. Use for new or materially changed pages under service-web or docs/service-web/. For every new page, require a visual prototype based on repository UI assets and the quant-v2-ui-design skill before production implementation.
---

# Service Web Solution

Turn understood business needs into reviewable page architecture, visual prototypes, and implementation-ready Web plans.

## Mandatory bases

1. Read and follow `../technical-solution/SKILL.md` first, including its standard and HTML template.
2. Read and follow `../quant-v2-ui-design/SKILL.md` for every page or component design.
3. Apply this skill as the service-web overlay. Resolve conflicts in this order: user request, nearest `AGENTS.md`, this skill, UI skill, base solution skill.
4. Place the canonical proposal under `docs/service-web/<NNNN>-<topic>/`.
5. Use available `mui` and `vercel-react-best-practices` skills when MUI implementation or React performance is in scope.
6. When the design changes an API or data-sync boundary, also load that service's solution skill and keep one named owning proposal plus linked impact/contract changes.

## Workflow

1. Read `service-web/README.md`, routes, affected views/components, query clients, types, fixtures, tokens, theme, chart tokens, and existing related pages. Use CodeGraph first when indexed.
2. Write a business-understanding brief before choosing layout:
   - actor and entry point;
   - primary business question and decision;
   - primary task, secondary tasks, success, and failure;
   - required data, authoritative source, freshness, permissions, and sensitivity;
   - share/bookmark needs, device context, and accessibility constraints.
3. Mark unknowns as assumptions or ask one focused question when an answer would materially change information architecture.
4. Classify the work:
   - **new page**: new route, new primary task, or materially new information architecture;
   - **existing page change**: preserves route, primary question, and information hierarchy.
5. For a new page, complete the prototype gate in `references/prototype-standard.md` before writing production page code:
   - reuse repository tokens and established page patterns;
   - create editable `prototype.html`;
   - render desktop and mobile prototype images;
   - visually inspect hierarchy, overflow, states, semantics, and accessibility;
   - embed or link the prototype images from the technical proposal.
6. Design page architecture:
   - route, shell, information order, page sections, component boundaries, and interactions;
   - loading, empty, stale, partial, error/retry, forbidden, disabled, submitting, and success states;
   - desktop, tablet, mobile, keyboard, focus, reduced-motion, and table/chart behavior.
7. Design state ownership:
   - remote server state in TanStack Query;
   - shareable filters and navigation state in URL;
   - local ephemeral interaction state in components;
   - high-frequency chart state in the chart engine instance.
8. Design API integration only from frozen contracts. For unfrozen APIs, use clearly labeled fixture or MSW and propose the contract separately; never invent a live endpoint.
9. Design performance: route lazy loading, query caching, payload bounds, render isolation, chart lifecycle, bundle impact, and perceived-loading behavior. Give measurable budgets or mark them unresolved with an owner.
10. Read `references/design-checklist.md` and `references/prototype-standard.md` completely before finalizing.
11. Validate proposal HTML, prototype source/images, desktop/mobile rendering, and local links. Execute document/prototype checks; list `vp check`, tests, build, and E2E as planned/not run when no production implementation exists. Do not implement unless the user also requests implementation.

## Prototype gate

- Treat prototype production and visual inspection as mandatory for new pages.
- Do not use generic AI dashboard imagery or ImageGen for product UI.
- Do not substitute a text wireframe for the required visual prototype.
- Do not write production page code before the prototype exists and has been reviewed.
- If implementation is requested in the same task and no material decision remains open, continue after documenting the prototype review; otherwise request the missing decision.

## Required proposal content

Include, when applicable:

- business-understanding brief and new/existing classification;
- information architecture and primary/secondary task hierarchy;
- prototype images for every new page, with source and review notes;
- route/component/query/state ownership map;
- API/fixture strategy and contract dependencies;
- state, responsive, accessibility, performance, rollout, rollback, and acceptance design.

## Repository guardrails

- Keep China-market red-up/green-down semantics and pair color with text/sign.
- Keep KLineChart for K-line, indicators, and overlays; use ECharts only for non-K-line analytics.
- Keep server state in TanStack Query and chart interaction state in the engine.
- Keep service-web behind service-api; never access databases or providers directly.
- Do not guess unfrozen endpoints, real-time protocols, permissions, or financial semantics.
- Do not replace the product homepage market overview with personal portfolio content.
