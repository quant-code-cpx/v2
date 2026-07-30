import { useState } from "react";
import type { SyntheticEvent } from "react";
import { Box, Card, CardContent, Divider, Stack, Tab, Tabs, Typography } from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import { MarketDirectionalValue } from "../../../components/MarketDirectionalValue";
import type { MarketOverview } from "../../../types/market";
import { formatCnyYi, formatCoverageRatio } from "../../../utils/market-formatters";

type EquityRankingKey = keyof MarketOverview["equityRankings"];
type MoneyFlowDirection = "inflow" | "outflow";

const equityRankingLabels: Record<EquityRankingKey, string> = {
  gainers: "涨幅",
  losers: "跌幅",
  amount: "成交额",
  turnover: "换手",
};

/** 渲染一条股票市场摘要，并深链 canonical 0007 身份路由。 */
function EquityRankRow({ item }: { item: MarketOverview["equityRankings"]["gainers"][number] }) {
  return (
    <Stack
      component={RouterLink}
      to={`/market/equities/${item.exchange}/${item.symbol}`}
      direction="row"
      justifyContent="space-between"
      alignItems="center"
      spacing={2}
      sx={{
        py: 1,
        color: "inherit",
        textDecoration: "none",
        "&:hover": { color: "primary.main" },
        "&:focus-visible": { outline: 2, outlineColor: "primary.main", outlineOffset: 2 },
      }}
    >
      <Box minWidth={0}>
        <Typography variant="body2" fontWeight={700} noWrap>
          {item.rank}. {item.name}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {item.exchange} · {item.symbol} · {formatCnyYi(item.amountCny)}
        </Typography>
      </Box>
      <MarketDirectionalValue value={item.changePercent} variant="compact" />
    </Stack>
  );
}

/** 渲染首页股票涨跌、成交额或换手摘要页签。 */
function EquityRankingCard({ overview }: { overview: MarketOverview }) {
  const [metric, setMetric] = useState<EquityRankingKey>("gainers");

  /** 切换首页摘要维度，不改变服务端完整包或触发个股详情请求。 */
  function handleMetricChange(_event: SyntheticEvent, nextMetric: EquityRankingKey): void {
    setMetric(nextMetric);
  }

  const items = overview.equityRankings[metric].slice(0, 5);
  return (
    <Card>
      <CardContent>
        <Typography variant="h6">股票排行摘要</Typography>
        <Tabs
          value={metric}
          onChange={handleMetricChange}
          aria-label="股票排行维度"
          sx={{ mt: 0.5 }}
        >
          {Object.entries(equityRankingLabels).map(
            /** 将固定排行维度渲染为本地页签，不改变 API 排名口径。 */
            ([value, label]) => (
              <Tab key={value} value={value} label={label} />
            ),
          )}
        </Tabs>
        {items.length === 0 ? (
          <Typography variant="body2" color="text.secondary" sx={{ py: 3 }}>
            当前 publication 未包含该排行记录。
          </Typography>
        ) : (
          <Stack divider={<Divider flexItem />}>
            {items.map(
              /** 使用交易所与代码双身份作为稳定 React key。 */
              (item) => (
                <EquityRankRow key={`${item.exchange}:${item.symbol}`} item={item} />
              ),
            )}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}

/** 渲染一条供应商订单规模资金流排行，不与其他方法学混合比较。 */
function MoneyFlowRankRow({
  item,
}: {
  item: MarketOverview["equityMoneyFlowRankings"]["inflow"][number];
}) {
  return (
    <Stack
      component={RouterLink}
      to={`/market/equities/${item.exchange}/${item.symbol}`}
      direction="row"
      justifyContent="space-between"
      alignItems="center"
      spacing={2}
      sx={{
        py: 1,
        color: "inherit",
        textDecoration: "none",
        "&:hover": { color: "primary.main" },
      }}
    >
      <Box minWidth={0}>
        <Typography variant="body2" fontWeight={700} noWrap>
          {item.rank}. {item.name}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {item.exchange} · {item.symbol}
        </Typography>
      </Box>
      <Typography
        variant="body2"
        fontWeight={700}
        color={
          Number(item.netAmountCny) > 0
            ? "error.main"
            : Number(item.netAmountCny) < 0
              ? "success.main"
              : "text.secondary"
        }
        sx={{ fontVariantNumeric: "tabular-nums" }}
      >
        {Number(item.netAmountCny) > 0 ? "+" : ""}
        {formatCnyYi(item.netAmountCny)}
      </Typography>
    </Stack>
  );
}

/** 渲染资金流入或流出摘要，并显式展示供应商覆盖率。 */
function MoneyFlowRankingCard({ overview }: { overview: MarketOverview }) {
  const [direction, setDirection] = useState<MoneyFlowDirection>("inflow");

  /** 切换同一资金流 publication 内的流入与流出列表。 */
  function handleDirectionChange(_event: SyntheticEvent, nextDirection: MoneyFlowDirection): void {
    setDirection(nextDirection);
  }

  const rankings = overview.equityMoneyFlowRankings;
  return (
    <Card>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="baseline">
          <Typography variant="h6">供应商个股资金流</Typography>
          <Typography variant="caption" color="text.secondary">
            覆盖 {formatCoverageRatio(rankings.coverage)}
          </Typography>
        </Stack>
        <Tabs
          value={direction}
          onChange={handleDirectionChange}
          aria-label="供应商资金流方向"
          sx={{ mt: 0.5 }}
        >
          <Tab value="inflow" label="净流入" />
          <Tab value="outflow" label="净流出" />
        </Tabs>
        {rankings[direction].length === 0 ? (
          <Typography variant="body2" color="text.secondary" sx={{ py: 3 }}>
            当前 publication 未包含该方向记录。
          </Typography>
        ) : (
          <Stack divider={<Divider flexItem />}>
            {rankings[direction].slice(0, 5).map(
              /** 资金流列表同样使用完整公开证券身份去重。 */
              (item) => (
                <MoneyFlowRankRow key={`${item.exchange}:${item.symbol}`} item={item} />
              ),
            )}
          </Stack>
        )}
        <Typography variant="caption" color="text.secondary">
          {rankings.methodologyId} v{rankings.methodologyVersion} · {rankings.universe}
        </Typography>
      </CardContent>
    </Card>
  );
}

/** 组合股票市场与供应商资金流两类不可混排摘要。 */
export function MarketRankingCards({ overview }: { overview: MarketOverview }) {
  return (
    <Box
      component="section"
      aria-label="股票市场摘要排行"
      sx={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 2 }}
    >
      <EquityRankingCard overview={overview} />
      <MoneyFlowRankingCard overview={overview} />
    </Box>
  );
}
