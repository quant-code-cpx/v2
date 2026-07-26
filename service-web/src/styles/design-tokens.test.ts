import { describe, expect, it } from "vite-plus/test";

import {
  appLayout,
  brandColors,
  componentGeometry,
  feedbackColors,
  marketColors,
} from "./design-tokens";
import { createAppTheme } from "./theme";

describe("quant-v2 design tokens", () => {
  it("keeps China-market directional semantics explicit", () => {
    expect(marketColors.up).toBe(feedbackColors.error);
    expect(marketColors.down).toBe(feedbackColors.success);
    expect(marketColors.up).not.toBe(marketColors.down);
  });

  it("keeps the measured shell and component geometry", () => {
    expect(appLayout.sidebarWidth).toBe(300);
    expect(appLayout.appBarDesktopHeight).toBe(72);
    expect(componentGeometry.cardRadius).toBe(16);
    expect(componentGeometry.fieldHeight).toBe(56);
    expect(componentGeometry.drawerWidth).toBe(360);
  });

  it("maps tokens into the active MUI theme", () => {
    const theme = createAppTheme("light");

    expect(theme.palette.primary.main).toBe(brandColors.primary);
    expect(theme.palette.error.main).toBe(marketColors.up);
    expect(theme.palette.success.main).toBe(marketColors.down);
    expect(theme.shape.borderRadius).toBe(componentGeometry.baseRadius);
  });
});
