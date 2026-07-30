import { Card, CardContent, Chip, Stack, Typography } from "@mui/material";
import { Box } from "@mui/material";

import type { StockConnectChannelSummary } from "../../../types/stock-connect";
import {
  stockConnectOrderAcceptanceLabel,
  stockConnectQuotaLabel,
  stockConnectSessionLabel,
} from "../utils/stock-connect-presentation";
import { MoneyFact } from "./MoneyFact";

/** 描述单通道日终事实指标区。 */
interface ChannelDetailMetricsProps {
  summary: StockConnectChannelSummary;
}

/** 显示买入、卖出、成交额、净额和额度五个独立事实，不混淆量纲。 */
export function ChannelDetailMetrics({ summary }: ChannelDetailMetricsProps) {
  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: "repeat(5, minmax(0, 1fr))",
        gap: 2,
      }}
    >
      <Card>
        <CardContent>
          <MoneyFact label="买入金额" fact={summary.stats.buyAmount} variant="amount" />
        </CardContent>
      </Card>
      <Card>
        <CardContent>
          <MoneyFact label="卖出金额" fact={summary.stats.sellAmount} variant="amount" />
        </CardContent>
      </Card>
      <Card>
        <CardContent>
          <MoneyFact label="通道成交额" fact={summary.stats.turnoverAmount} variant="amount" />
          <Typography variant="caption" color="text.secondary">
            中性成交事实 · 非资金流
          </Typography>
        </CardContent>
      </Card>
      <Card>
        <CardContent>
          <MoneyFact label="净额" fact={summary.stats.netBuyAmount} variant="net" />
        </CardContent>
      </Card>
      <Card>
        <CardContent>
          <Stack spacing={1}>
            <Stack direction="row" justifyContent="space-between" spacing={1}>
              <Typography variant="caption" color="text.secondary">
                额度与状态
              </Typography>
              <Chip
                size="small"
                color={summary.status.sessionState === "UNKNOWN" ? "warning" : "info"}
                label={stockConnectSessionLabel(summary.status)}
              />
            </Stack>
            <Typography variant="h6">{stockConnectQuotaLabel(summary.status)}</Typography>
            <Typography variant="caption" color="text.secondary">
              额度币种独立于南向成交币种 · 仅日终
            </Typography>
            <Typography variant="caption" color="text.secondary">
              买入委托：{stockConnectOrderAcceptanceLabel(summary.status.buyOrderAccepted)}
              {" · "}
              卖出委托：{stockConnectOrderAcceptanceLabel(summary.status.sellOrderAccepted)}
            </Typography>
          </Stack>
        </CardContent>
      </Card>
    </Box>
  );
}
