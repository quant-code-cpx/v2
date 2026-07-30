import { Box, Card, CardContent, Chip, Stack, Typography } from "@mui/material";

import type { EtfProfileValues } from "../../../types/etf";
import {
  etfExchangeLabel,
  etfListingStatusLabel,
  formatEtfDate,
} from "../../../utils/etf-presentation";

/** ETF 产品资料卡所需的严格 profile v2 字段。 */
interface EtfProfileSummaryProps {
  profile: EtfProfileValues;
}

/** 展示来源明确的 ETF 身份与基本资料，并对未接入跟踪指数明确降级。 */
export function EtfProfileSummary({ profile }: EtfProfileSummaryProps) {
  const facts = [
    ["交易所", etfExchangeLabel(profile.exchange)],
    ["ETF 类型", profile.etfType],
    ["管理方式", profile.managementMode],
    ["基金管理人", profile.managerName ?? "未披露"],
    ["基金托管人", profile.custodianName ?? "未披露"],
    ["上市日期", formatEtfDate(profile.listedOn)],
    ["报价币种", profile.quoteCurrency],
    ["NAV 币种", profile.navCurrency],
  ] as const;

  return (
    <Card component="section">
      <CardContent sx={{ p: 3 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
          <Box>
            <Typography component="h2" variant="h6">
              产品资料
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              产品目录 v2 · 时间精度 {profile.sourceTimePrecision}
            </Typography>
          </Box>
          <Chip
            variant="outlined"
            color={profile.listingStatus === "UNKNOWN" ? "warning" : "primary"}
            label={etfListingStatusLabel(profile.listingStatus)}
          />
        </Stack>
        <Box
          component="dl"
          sx={{
            display: "grid",
            gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
            gap: 2.5,
            m: 0,
            mt: 3,
          }}
        >
          {/* 产品事实严格来自 profile values，空值保持“未披露”。 */}
          {facts.map(([label, value]) => (
            <Box key={label}>
              <Typography component="dt" variant="caption" color="text.secondary">
                {label}
              </Typography>
              <Typography component="dd" sx={{ m: 0, mt: 0.5, fontWeight: 700 }}>
                {value}
              </Typography>
            </Box>
          ))}
          <Box>
            <Typography component="dt" variant="caption" color="text.secondary">
              跟踪指数
            </Typography>
            <Typography component="dd" sx={{ m: 0, mt: 0.5, color: "text.secondary" }}>
              数据集未接入
            </Typography>
          </Box>
        </Box>
      </CardContent>
    </Card>
  );
}
