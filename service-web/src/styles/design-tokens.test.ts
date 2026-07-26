import { describe, expect, it } from "vite-plus/test";

import {
  appLayout,
  brandColors,
  chartColors,
  componentGeometry,
  feedbackColors,
  interactionColors,
  marketColors,
  neutralColors,
} from "./design-tokens";
import { createAppTheme } from "./theme";

// Group callbacks that guard product design-system invariants from accidental visual drift.
describe("quant-v2 design tokens", () => {
  // Verify China-market gain/loss colors preserve their non-Western semantics.
  it("keeps China-market directional semantics explicit", () => {
    expect(marketColors.up).toBe(feedbackColors.error);
    expect(marketColors.down).toBe(feedbackColors.success);
    expect(marketColors.up).not.toBe(marketColors.down);
  });

  // Verify directional colors cannot silently leak into brand or analytical chart tokens.
  it("keeps brand and non-directional chart colors separate from market direction", () => {
    expect(brandColors.primary).toBe("#6D5CE7");
    expect(brandColors.secondary).toBe("#2F6BFF");
    expect(Object.values(brandColors)).not.toContain(marketColors.down);
    expect(Object.values(chartColors)).not.toContain(marketColors.up);
    expect(Object.values(chartColors)).not.toContain(marketColors.down);
    expect(interactionColors.focusRingShadow).toContain("109 92 231");
  });

  // Verify layout dimensions remain aligned with approved UI measurements.
  it("keeps the measured shell and component geometry", () => {
    expect(appLayout.sidebarWidth).toBe(300);
    expect(appLayout.appBarDesktopHeight).toBe(72);
    expect(componentGeometry.cardRadius).toBe(16);
    expect(componentGeometry.fieldHeight).toBe(56);
    expect(componentGeometry.drawerWidth).toBe(360);
  });

  // Verify theme factory exposes source design tokens through MUI's semantic palette.
  it("maps tokens into the active MUI theme", () => {
    const theme = createAppTheme("light");

    expect(theme.palette.primary.main).toBe(brandColors.primary);
    expect(theme.palette.secondary.main).toBe(brandColors.secondary);
    expect(theme.palette.info.contrastText).toBe(neutralColors[900]);
    expect(theme.palette.error.main).toBe(marketColors.up);
    expect(theme.palette.success.main).toBe(marketColors.down);
    expect(theme.shape.borderRadius).toBe(componentGeometry.baseRadius);
  });
});
