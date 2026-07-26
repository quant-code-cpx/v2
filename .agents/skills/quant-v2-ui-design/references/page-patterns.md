# QUANT V2 page patterns

## Product homepage: today’s market

Primary question: “What happened in the market today, and what needs attention?”

Recommended desktop order:

1. Page heading: `今日市场`, trading status, market selector, refresh action.
2. Four index cards: 上证指数、深证成指、沪深300、创业板指.
3. Eight-column intraday market chart plus four-column market breadth/flow card.
4. Seven-column sector performance plus five-column anomaly feed.
5. Personal portfolio and strategy summaries only after market state, or on dedicated routes.

Show at a glance:

- index level and percentage;
- advancing/declining counts;
- limit-up/limit-down counts;
- market turnover and change from previous session;
- northbound/main capital flow when available;
- strongest and weakest sectors;
- material anomalies and risk signals.

Avoid greeting heroes, generic illustrations, or asset summaries above today’s market state.

## Analysis page

- Use 1536px analytics container.
- Keep instrument identity and current quote in a compact header.
- Put chart controls adjacent to chart, not in a detached toolbar.
- Give K-line chart the largest surface.
- Place factors, fundamentals, and signals in secondary cards.
- Keep date range, adjustment mode, and interval visible.

## List and filtered table

Order:

1. Page title and primary action.
2. Tabs for stable dataset partitions.
3. Filter row or neutral filter band.
4. Result count and bulk actions.
5. Table head, rows, pagination.

Rules:

- Table head: 56px, `#F4F6F8`.
- Standard row: 76px; dense market row: 52px.
- Use faint dashed horizontal dividers and no vertical rules.
- Align numbers right; keep names and symbols left.
- Keep sticky identity columns only when they materially help.
- Keep table minimum width around 800px on mobile and allow horizontal scroll.
- Stack filters at narrow widths; do not squeeze fields below usable size.
- Preserve filter state in URL when users may share or revisit results.

## Form page

- Use regular 1200px container; keep primary form column 720–960px.
- Group fields by user task, not by backend object shape.
- Use one card per coherent section.
- Card padding and section gap: 24px.
- Field height: 56px.
- Use labels for field meaning and helper text for examples.
- Keep validation near the field.
- Put destructive actions away from primary submit.
- On mobile, stack all fields and keep the submit action reachable.

Required states:

- default, hover, focused, filled, disabled, error, submitting, success.

## Metric cards

- One metric and one comparison per card.
- Label first, value second, comparison third.
- Use a small sparkline only when it helps trend recognition.
- Do not fill the entire card red or green.
- Use directional soft backgrounds only for small badges or icon tiles.

## Chart cards

- Header contains title, short context, range or actions.
- Plot owns most of the card.
- Legend stays close to plot.
- Keep no more than four series without a strong reason.
- Use neutral comparison lines and semantic market color only when the series itself is directional.

## Drawer

Use for contextual side tasks that should preserve page state:

- filters;
- saved views;
- row detail;
- lightweight editing.

Geometry:

- width 360px or full viewport on small screens;
- white at 90% opacity with 20px blur;
- subtle left shadow;
- 24px content padding;
- header and footer actions remain visible when content scrolls.

Do not use a drawer for multi-step or destructive tasks.

## Dialog

Use for blocking confirmation or short focused work:

- destructive confirmation;
- rename/save view;
- one short form;
- irreversible business action.

Geometry:

- width up to 720px;
- 16px radius;
- title/content/actions padding 24px;
- action gap 12px.

Keep cancel before confirm. Make destructive confirm explicit and use error color only for destructive meaning.

## Responsive behavior

- Desktop: sidebar + 12-column content.
- Tablet: collapse secondary columns before shrinking content illegibly.
- Mobile: 16px page padding, single-column cards, 64px header.
- Preserve horizontal table scrolling.
- Move drawers to full width when needed.
- Keep chart controls wrap-safe; never overlap the plot.

## State checklist

For remote data, provide:

- skeleton loading matching final geometry;
- empty state with next action;
- recoverable error with retry;
- stale-data timestamp;
- partial-data warning when one provider fails;
- permission-denied state without exposing restricted content.
