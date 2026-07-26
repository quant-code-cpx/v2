# QUANT V2 design language

## Principles

1. Use neutral color to build structure; reserve vivid color for meaning.
2. Make data primary and explanation secondary.
3. Use regular density, not arbitrary whitespace.
4. Give each card one coherent task.
5. Keep desktop efficient and mobile touchable.
6. Treat this as the initial product baseline.

## Color

Canonical values live in:

- `service-web/src/styles/design-tokens.css`
- `service-web/src/styles/design-tokens.ts`

Core palette:

- Canvas and Paper: `#FFFFFF`
- Neutral background: `#F4F6F8`
- Subtle background: `#F9FAFB`
- Primary text: `#1C252E`
- Secondary text: `#637381`
- Disabled text: `#919EAB`
- Divider: `rgb(145 158 171 / 20%)`
- Primary: `#00A76F`
- Primary dark: `#007867`
- Secondary: `#8E33FF`
- Info: `#00B8D9`
- Warning: `#FFAB00`
- Error: `#FF5630`
- Success: `#22C55E`

China-market override:

- Up/positive: `#FF5630`
- Down/negative: `#22C55E`
- Flat: `#637381`
- Pair every directional color with `+`, `−`, arrow, or explicit label.

Do not use market red/green for category identity. Use teal, cyan, purple, amber, and neutral gray for non-directional series.

## Typography

- Body: Public Sans Variable with PingFang SC and Microsoft YaHei fallbacks.
- Display headings: Barlow with the same Chinese fallbacks.
- Financial numbers: tabular numerals; use mono only for code-like identifiers.
- Page title: usually 24/36, weight 700.
- Card title: 17–18px, weight 600–700.
- Body: 16/24.
- Body small: 14/22.
- Caption: 12/18.
- Button: 14/24, weight 700.

Avoid thin weights and oversized dashboard headings.

## Spacing and shape

- Base grid: 8px.
- Half-step: 4px.
- Common values: 8, 12, 16, 20, 24, 32, 40, 48, 64.
- Card padding: 24px.
- Grid gap: 24px.
- Control radius: 8px.
- Card/dialog radius: 16px.
- Label/chip radius: 6px.
- Circular controls stay circular.

Card shadow:

```css
0 0 2px 0 rgb(145 158 171 / 20%),
0 12px 24px -4px rgb(145 158 171 / 12%)
```

Prefer shadow or subtle divider; rarely use both.

## Layout

- Desktop sidebar: 300px.
- Desktop app bar: 72px.
- Mobile app bar: 64px.
- Regular content max width: 1200px including 40px side padding.
- Analytics content max width: 1536px including 40px side padding.
- Mobile page padding: 16px.
- Main grids: 12 columns, 24px gap.

Use the 1536px analytics grid for market dashboards and chart-heavy analysis. Use the 1200px grid for forms and standard management pages.

## MUI geometry

- Small button: 32px.
- Medium button: 36px.
- Large button: 48px.
- IconButton: 40px.
- Outlined field: 56px.
- Tab: 42px.
- Chip: 24px.
- Table head: 56px.
- Standard table row: 76px.
- Dense market row: 52px.
- Drawer: 360px.
- Medium dialog: up to 720px.

## Surfaces

- Default shell stays white.
- Use `#F4F6F8` for filter bands, selected controls, table heads, and nested neutral zones.
- Use `#F9FAFB` for weaker hierarchy.
- Dark surfaces are allowed only for bounded media or one strong focal card. They must not become the sidebar, header, or page background.

## Charts

- Keep plot background transparent or Paper white.
- Use faint dashed grid lines.
- Keep axes and legends secondary.
- Use one primary series and one quiet comparison by default.
- Use area fills at low opacity.
- Keep tooltip values tabular and directional signs explicit.
- Do not use 3D charts, heavy gradients, or decorative glow.

## Interaction and accessibility

- Every clickable element needs hover, active, focus, disabled, and loading behavior where relevant.
- Focus ring: `0 0 0 3px rgb(0 167 111 / 28%)`.
- Minimum touch target: 40px; use 44–48px for mobile primary controls.
- Target WCAG 2.2 AA.
- Never rely on color alone.
- Honor reduced motion.
