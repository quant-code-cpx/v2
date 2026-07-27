import { ShowChartRounded as ShowChartRoundedIcon } from "@mui/icons-material";
import { Box, Card, CardContent, Chip, Divider, Skeleton, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { marketOverviewQueryOptions } from "../../api/market";
import { ChangeValue } from "./components/ChangeValue";
import { MarketMoverRow } from "./components/MarketMoverRow";
import {
  compactMarketNumberFormatter,
  formatMarketUpdatedTime,
  marketNumberFormatter,
} from "./utils/market-formatters";

/** 路由查询数据可用后渲染受控 fixture 市场概览。 */
export function MarketOverviewView() {
  const { data, isPending } = useQuery(marketOverviewQueryOptions);

  if (isPending || data === undefined) {
    return <MarketOverviewLoading />;
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
                  {marketNumberFormatter.format(data.indexValue)}
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
              {compactMarketNumberFormatter.format(data.turnover)}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              更新：{formatMarketUpdatedTime(data.updatedAt)}
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
            {/* 将每个 API 活跃标的投影为可访问的标的导航行。 */}
            {data.movers.map((mover) => (
              <MarketMoverRow key={mover.symbol} mover={mover} />
            ))}
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}

/** 查询缓存填充期间在页面模块内保留市场概览几何。 */
function MarketOverviewLoading() {
  return (
    <Stack spacing={3}>
      <Skeleton variant="rounded" height={112} />
      <Skeleton variant="rounded" height={260} />
    </Stack>
  );
}
