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

// 汇集防止产品设计系统意外漂移的断言。
describe("Apex design tokens", () => {
  // 验证中国市场涨跌颜色保留约定俗成的语义。
  it("keeps China-market directional semantics explicit", () => {
    expect(marketColors.up).toBe(feedbackColors.error);
    expect(marketColors.down).toBe(feedbackColors.success);
    expect(marketColors.up).not.toBe(marketColors.down);
  });

  // 验证方向色不会静默混入品牌或非方向性图表令牌。
  it("keeps brand and non-directional chart colors separate from market direction", () => {
    expect(brandColors.primary).toBe("#6D5CE7");
    expect(brandColors.secondary).toBe("#2F6BFF");
    expect(Object.values(brandColors)).not.toContain(marketColors.down);
    expect(Object.values(chartColors)).not.toContain(marketColors.up);
    expect(Object.values(chartColors)).not.toContain(marketColors.down);
    expect(interactionColors.focusRingShadow).toContain("109 92 231");
  });

  // 验证布局尺寸保持在已批准的界面度量内。
  it("keeps the measured shell and component geometry", () => {
    expect(appLayout.desktopMinWidth).toBe(1200);
    expect(appLayout.sidebarWidth).toBe(300);
    expect(appLayout.appBarDesktopHeight).toBe(72);
    expect(componentGeometry.cardRadius).toBe(16);
    expect(componentGeometry.fieldHeight).toBe(56);
    expect(componentGeometry.drawerWidth).toBe(360);
  });

  // 验证主题工厂始终输出已批准的浅色桌面主题，并映射设计令牌。
  it("maps tokens into the approved light MUI theme", () => {
    const theme = createAppTheme();

    expect(theme.palette.mode).toBe("light");
    expect(theme.palette.primary.main).toBe(brandColors.primary);
    expect(theme.palette.secondary.main).toBe(brandColors.secondary);
    expect(theme.palette.info.contrastText).toBe(neutralColors[900]);
    expect(theme.palette.error.main).toBe(marketColors.up);
    expect(theme.palette.success.main).toBe(marketColors.down);
    expect(theme.shape.borderRadius).toBe(componentGeometry.baseRadius);
    expect(theme.components?.MuiOutlinedInput?.styleOverrides?.input).toMatchObject({
      "&:focus-visible": { boxShadow: "none" },
    });
  });
});
