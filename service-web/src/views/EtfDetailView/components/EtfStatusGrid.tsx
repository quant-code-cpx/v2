import { Box, Chip, Stack, Typography } from "@mui/material";

import type { EtfStateDimension, EtfTradingStateValues } from "../../../types/etf";
import { etfStateDimensionLabel } from "../../../utils/etf-presentation";
import { latestEtfStates } from "../utils/etf-detail";

/** ETF 状态区块只消费已严格校验的状态 values。 */
interface EtfStatusGridProps {
  values: readonly EtfTradingStateValues[];
}

/** 三个状态维度固定顺序展示，缺失维度保持未发布而不相互推导。 */
const dimensions: readonly EtfStateDimension[] = ["TRADING", "SUBSCRIPTION", "REDEMPTION"];

/** 展示来源最近报告的交易、申购和赎回独立状态。 */
export function EtfStatusGrid({ values }: EtfStatusGridProps) {
  const latest = latestEtfStates(values);

  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
        gap: 2,
      }}
    >
      {/* 缺失维度明确显示未发布，不能用上市状态或其他维度补齐。 */}
      {dimensions.map((dimension) => {
        const status = latest.get(dimension);

        return (
          <Box key={dimension} sx={{ p: 2, borderRadius: 2, bgcolor: "grey.100" }}>
            <Typography variant="caption" color="text.secondary">
              {etfStateDimensionLabel(dimension)}（最近报告）
            </Typography>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1 }}>
              <Chip
                size="small"
                variant="outlined"
                color={status === undefined ? "default" : "primary"}
                label={status?.state ?? "未发布"}
              />
              <Typography variant="caption" color="text.secondary">
                {status?.effectiveFrom ?? "无生效日期"}
              </Typography>
            </Stack>
            {status?.reason === null || status === undefined ? null : (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                {status.reason}
              </Typography>
            )}
          </Box>
        );
      })}
    </Box>
  );
}
