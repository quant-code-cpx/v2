---
name: quant-v2-ui-design
description: Design, implement, review, or revise QUANT V2 product UI using the repository's Minimal-inspired design system and China-market semantics. Use for service-web pages, dashboards, MUI components, CSS or theme work, charts, cards, tables, filters, forms, drawers, dialogs, responsive behavior, accessibility, or visual QA.
---

# QUANT V2 UI Design

Build a calm, data-first quant product. Treat this skill as the first product UI baseline, not a migration from a previous design system.

## Load the right sources

Before UI work, read:

- `service-web/src/styles/design-tokens.css` for canonical CSS variables.
- `service-web/src/styles/design-tokens.ts` for TypeScript tokens.
- `service-web/src/styles/theme.tsx` when using or changing MUI.
- `service-web/src/styles/chart-tokens.ts` when changing charts.
- [references/design-language.md](references/design-language.md) for visual rules.
- [references/page-patterns.md](references/page-patterns.md) for page and component composition.

Read `docs/service-web/0002-minimal-inspired-design-system/index.html` only when exact research evidence, comparison rationale, or visual examples are needed.

## Workflow

1. Identify user task and page archetype: market overview, analysis, list/table, form, drawer, or dialog.
2. Order information by decision value before styling it. Put today’s market state before personal portfolio data on the product homepage.
3. Compose with MUI and repository tokens. Reuse theme values; do not scatter hex colors, shadows, radii, or random pixel spacing.
4. Keep white canvas and low-contrast gray hierarchy. Use dark or image-heavy surfaces only as bounded content, never as the app shell.
5. Apply China-market semantics: up is red, down is green. Always pair color with sign, arrow, or text.
6. Use non-directional chart colors for categories and comparisons. Do not reuse market red/green as arbitrary decoration.
7. Implement mobile and keyboard behavior with desktop layout. Preserve touch targets, horizontal table access, focus states, loading, empty, error, and disabled states.
8. Run `pnpm check`, affected tests, and `pnpm build` in `service-web`.

## Implementation rules

- Prefer MUI components and typed `SxProps<Theme>`.
- Use `theme.spacing()` or the 8px/4px token scale.
- Use `Card` for one coherent task, not every visual group.
- Use `useChartVisualTokens()` for ECharts and KLine visual integration.
- Import MUI from package exports; do not use deep imports.
- Keep complex styles outside components once they exceed roughly 100 lines.
- Keep directional values tabular and right-aligned in tables.
- Use semantic HTML, accessible names, visible focus, and WCAG 2.2 AA contrast.
- Reduce motion when `prefers-reduced-motion` requests it.

## Non-negotiable guardrails

- No dark sidebar or large black-and-white shell contrast.
- No decorative gradients, neon glow, glassmorphism, or heavy borders.
- No random radii; controls use 8px, cards/dialogs use 16px, labels use 6px.
- No color-only financial meaning.
- No giant welcome hero on information-critical quant pages.
- No personal portfolio summary replacing the market overview on the homepage.
- No new visual constant outside canonical token files unless the value is inherently local.

## Completion check

Confirm:

- hierarchy answers the page’s primary business question quickly;
- spacing follows 4px/8px steps;
- components match tokenized dimensions;
- red-up/green-down semantics are correct;
- empty/loading/error states exist where data is remote;
- desktop, tablet, and mobile layouts remain usable;
- no unrelated visual constants were introduced.
