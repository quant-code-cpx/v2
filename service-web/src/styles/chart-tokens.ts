import { useMemo } from "react";
import { useTheme } from "@mui/material/styles";

import { chartColors, marketColors } from "./design-tokens";

export interface ChartVisualTokens {
  background: string;
  textPrimary: string;
  textSecondary: string;
  gridLine: string;
  positive: string;
  negative: string;
  neutral: string;
  series: readonly string[];
}

/** Map active MUI palette and semantic tokens into chart-engine color contract. */
export function useChartVisualTokens(): ChartVisualTokens {
  const theme = useTheme();

  // Keep token object referentially stable so chart effects run only for visual changes.
  return useMemo(
    () => ({
      background: theme.palette.background.paper,
      textPrimary: theme.palette.text.primary,
      textSecondary: theme.palette.text.secondary,
      gridLine: theme.palette.divider,
      positive: marketColors.up,
      negative: marketColors.down,
      neutral: marketColors.flat,
      series: [chartColors.primary, chartColors.secondary, chartColors.accent, chartColors.warning],
    }),
    [theme],
  );
}
