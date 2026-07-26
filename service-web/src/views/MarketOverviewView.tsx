import {
  ArrowDownwardRounded as ArrowDownwardRoundedIcon,
  ArrowUpwardRounded as ArrowUpwardRoundedIcon,
  ShowChartRounded as ShowChartRoundedIcon,
} from "@mui/icons-material";
import { Box, Card, CardContent, Chip, Divider, Skeleton, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { Link as RouterLink } from "react-router-dom";

import { marketOverviewQueryOptions } from "../api/market";
import type { MarketMover } from "../types/market";

const numberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});

const compactNumberFormatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Render China-market directional percentage using explicit icon and semantic color. */
function ChangeValue({ value }: { value: number }) {
  const positive = value >= 0;

  return (
    <Stack
      direction="row"
      spacing={0.25}
      alignItems="center"
      color={positive ? "error.main" : "success.main"}
    >
      {positive ? (
        <ArrowUpwardRoundedIcon fontSize="inherit" />
      ) : (
        <ArrowDownwardRoundedIcon fontSize="inherit" />
      )}
      <Typography component="span" fontWeight={700}>
        {`${positive ? "+" : ""}${value.toFixed(2)}%`}
      </Typography>
    </Stack>
  );
}

/** Render one navigable market mover row with compact turnover and signed change. */
function MarketMoverRow({ mover }: { mover: MarketMover }) {
  return (
    <Box
      component={RouterLink}
      to={`/instruments/${mover.symbol}`}
      sx={{
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) auto",
        gap: 2,
        alignItems: "center",
        py: 1.25,
        color: "inherit",
        textDecoration: "none",
        "&:hover": { color: "primary.main" },
      }}
    >
      <Box>
        <Typography fontWeight={700}>{mover.name}</Typography>
        <Typography variant="caption" color="text.secondary">
          {mover.symbol} · 成交额 {compactNumberFormatter.format(mover.turnover)}
        </Typography>
      </Box>
      <Box textAlign="right">
        <Typography fontWeight={700}>{numberFormatter.format(mover.price)}</Typography>
        <ChangeValue value={mover.changePercent} />
      </Box>
    </Box>
  );
}

/** Render stable overview skeleton while query cache is populated. */
function OverviewLoading() {
  return (
    <Stack spacing={3}>
      <Skeleton variant="rounded" height={112} />
      <Skeleton variant="rounded" height={260} />
    </Stack>
  );
}

/** Render fixture-backed market summary once routed query data is available. */
export function MarketOverviewView() {
  const { data, isPending } = useQuery(marketOverviewQueryOptions);

  if (isPending || data === undefined) {
    return <OverviewLoading />;
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4">市场概览</Typography>
        <Typography color="text.secondary" sx={{ mt: 0.5 }}>
          数据接口接入前使用受控 fixture；真实行情将由 OpenAPI client 替换。
        </Typography>
      </Box>

      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "1.3fr 1fr 1fr",
          gap: 2,
        }}
      >
        <Card>
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
              <Box>
                <Typography color="text.secondary" variant="body2">
                  {data.indexName}
                </Typography>
                <Typography variant="h4" sx={{ mt: 1 }}>
                  {numberFormatter.format(data.indexValue)}
                </Typography>
              </Box>
              <Chip
                icon={<ShowChartRoundedIcon />}
                label="延迟数据"
                size="small"
                variant="outlined"
              />
            </Stack>
            <Box sx={{ mt: 1 }}>
              <ChangeValue value={data.indexChangePercent} />
            </Box>
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <Typography color="text.secondary" variant="body2">
              市场宽度
            </Typography>
            <Typography variant="h5" sx={{ mt: 1 }}>
              {data.advancing.toLocaleString()} / {data.declining.toLocaleString()}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              上涨 / 下跌家数
            </Typography>
          </CardContent>
        </Card>
        <Card>
          <CardContent>
            <Typography color="text.secondary" variant="body2">
              两市成交额
            </Typography>
            <Typography variant="h5" sx={{ mt: 1 }}>
              {compactNumberFormatter.format(data.turnover)}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              更新：
              {new Date(data.updatedAt).toLocaleTimeString("zh-CN", {
                hour: "2-digit",
                minute: "2-digit",
              })}
            </Typography>
          </CardContent>
        </Card>
      </Box>

      <Card>
        <CardContent>
          <Typography variant="h6">活跃标的</Typography>
          <Typography color="text.secondary" variant="body2" sx={{ mt: 0.5 }}>
            进入标的页验证 K 线与分析图表双引擎。
          </Typography>
          <Stack divider={<Divider flexItem />} sx={{ mt: 1 }}>
            {/* Project each API mover into its own accessible instrument navigation row. */}
            {data.movers.map((mover) => (
              <MarketMoverRow key={mover.symbol} mover={mover} />
            ))}
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
