import { InfoOutlined as InfoOutlinedIcon } from "@mui/icons-material";
import { Box, Card, CardContent, LinearProgress, Stack, Tooltip, Typography } from "@mui/material";

import { MarketDirectionalValue } from "../../../components/MarketDirectionalValue";
import { marketColors } from "../../../styles/design-tokens";
import type { MarketOverview } from "../../../types/market";
import { formatCnyTrillion, formatCnyYi } from "../../../utils/market-formatters";

/** 渲染沪深 A 股同口径股票横截面成交额，不引用指数成交额。 */
function TurnoverCard({ overview }: { overview: MarketOverview }) {
  return (
    <Card>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography fontWeight={700}>{overview.turnover.label}</Typography>
          <Tooltip
            title={`${overview.turnover.universe} · ${overview.turnover.methodologyId}；来自股票横截面聚合，不使用指数 daily amount。`}
          >
            <InfoOutlinedIcon fontSize="small" color="action" />
          </Tooltip>
        </Stack>
        <Typography variant="h5" sx={{ mt: 1, fontVariantNumeric: "tabular-nums" }}>
          {formatCnyTrillion(overview.turnover.totalAmountCny)}
        </Typography>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 0.75 }}>
          <Typography variant="body2" color="text.secondary">
            较前一交易日
          </Typography>
          <MarketDirectionalValue value={overview.turnover.changePercent} variant="compact" />
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          沪 {formatCnyYi(overview.turnover.sseAmountCny)} · 深{" "}
          {formatCnyYi(overview.turnover.szseAmountCny)}
        </Typography>
      </CardContent>
    </Card>
  );
}

/** 渲染全市场股票宽度，数值只来自同日股票横截面。 */
function BreadthCard({ overview }: { overview: MarketOverview }) {
  const traded = overview.breadth.advancing + overview.breadth.flat + overview.breadth.declining;
  const advancingRatio = traded === 0 ? 0 : (overview.breadth.advancing / traded) * 100;

  return (
    <Card>
      <CardContent>
        <Typography fontWeight={700}>市场宽度</Typography>
        <LinearProgress
          variant="determinate"
          value={advancingRatio}
          aria-label={`上涨家数占已交易股票 ${advancingRatio.toFixed(1)}%`}
          sx={{
            mt: 2,
            height: 8,
            borderRadius: 1,
            bgcolor: marketColors.downSoft,
            "& .MuiLinearProgress-bar": { bgcolor: "error.main" },
          }}
        />
        <Stack direction="row" justifyContent="space-between" sx={{ mt: 1.5 }}>
          <Box>
            <Typography color="error.main" fontWeight={700}>
              {overview.breadth.advancing.toLocaleString()}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              上涨
            </Typography>
          </Box>
          <Box textAlign="center">
            <Typography color="text.secondary" fontWeight={700}>
              {overview.breadth.flat.toLocaleString()}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              平盘
            </Typography>
          </Box>
          <Box textAlign="right">
            <Typography color="success.main" fontWeight={700}>
              {overview.breadth.declining.toLocaleString()}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              下跌
            </Typography>
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 渲染涨跌停计数和规则版本，禁止浏览器按价格重新判定。 */
function LimitsCard({ overview }: { overview: MarketOverview }) {
  return (
    <Card>
      <CardContent>
        <Typography fontWeight={700}>涨跌停</Typography>
        <Stack direction="row" spacing={3} sx={{ mt: 1.5 }}>
          <Box>
            <Typography variant="h5" color="error.main">
              {overview.limits.limitUp}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              涨停
            </Typography>
          </Box>
          <Box>
            <Typography variant="h5" color="success.main">
              {overview.limits.limitDown}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              跌停
            </Typography>
          </Box>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
          规则版本 {overview.limits.rulesVersion} · 停牌 {overview.breadth.suspended}
        </Typography>
      </CardContent>
    </Card>
  );
}

/** 渲染供应商方法学资金流，文案不将其宣称为统一市场事实。 */
function MoneyFlowCard({ overview }: { overview: MarketOverview }) {
  const flow = overview.marketMoneyFlow;
  return (
    <Card>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography fontWeight={700}>供应商市场资金流</Typography>
          <Tooltip
            title={`${flow.source.upstreamSource} · ${flow.source.sourceDataset}；${flow.methodologyId} v${flow.methodologyVersion}，只在该方法学内可比。`}
          >
            <InfoOutlinedIcon fontSize="small" color="action" />
          </Tooltip>
        </Stack>
        <Typography
          variant="h5"
          color={
            Number(flow.netAmountCny) > 0
              ? "error.main"
              : Number(flow.netAmountCny) < 0
                ? "success.main"
                : "text.secondary"
          }
          sx={{ mt: 1, fontVariantNumeric: "tabular-nums" }}
        >
          {Number(flow.netAmountCny) > 0 ? "+" : ""}
          {formatCnyYi(flow.netAmountCny)}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          Tushare Pro · {flow.source.upstreamSource} · 供应商方法学估算
        </Typography>
      </CardContent>
    </Card>
  );
}

/** 组合成交、宽度、涨跌停和供应商资金流四项市场体温。 */
export function MarketPulseGrid({ overview }: { overview: MarketOverview }) {
  return (
    <Box
      component="section"
      aria-label="市场体温"
      sx={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 2 }}
    >
      <TurnoverCard overview={overview} />
      <BreadthCard overview={overview} />
      <LimitsCard overview={overview} />
      <MoneyFlowCard overview={overview} />
    </Box>
  );
}
