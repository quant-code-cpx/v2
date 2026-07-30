import { Card, CardActionArea, CardContent, Chip, Divider, Stack, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import type { StockConnectChannelSummary } from "../../../types/stock-connect";
import {
  stockConnectChannelDescription,
  stockConnectChannelLabel,
  stockConnectQuotaLabel,
  stockConnectSessionLabel,
} from "../utils/stock-connect-presentation";
import { stockConnectChannelSlugByCode } from "../utils/stock-connect-url";
import { MoneyFact } from "./MoneyFact";

/** 描述总览四通道卡片及其当前 URL 查询字符串。 */
interface ChannelSummaryCardProps {
  summary: StockConnectChannelSummary;
  search: string;
}

/** 显示单通道成交额、净额、日终状态和额度，不进行跨币种合计。 */
export function ChannelSummaryCard({ summary, search }: ChannelSummaryCardProps) {
  const detailPath = `/market/stock-connect/${stockConnectChannelSlugByCode[summary.channel]}${search}`;

  return (
    <Card>
      <CardActionArea
        component={RouterLink}
        to={detailPath}
        aria-label={`查看${stockConnectChannelLabel(summary.channel)}通道详情`}
        sx={{ height: "100%", alignItems: "stretch" }}
      >
        <CardContent sx={{ height: "100%" }}>
          <Stack spacing={1.5}>
            <Stack direction="row" justifyContent="space-between" spacing={1}>
              <Stack>
                <Typography variant="h6">{stockConnectChannelLabel(summary.channel)}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {stockConnectChannelDescription(summary.channel)}
                </Typography>
              </Stack>
              <Chip
                size="small"
                color={summary.status.sessionState === "UNKNOWN" ? "warning" : "info"}
                label={stockConnectSessionLabel(summary.status)}
              />
            </Stack>
            <MoneyFact label="通道成交额" fact={summary.stats.turnoverAmount} variant="amount" />
            <Divider />
            <MoneyFact label="净额" fact={summary.stats.netBuyAmount} variant="net" />
            <Stack spacing={0.25}>
              <Typography variant="caption" color="text.secondary">
                {stockConnectQuotaLabel(summary.status)}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                官方来源活跃证券 {summary.activeSecurityCount} 只
              </Typography>
            </Stack>
          </Stack>
        </CardContent>
      </CardActionArea>
    </Card>
  );
}
