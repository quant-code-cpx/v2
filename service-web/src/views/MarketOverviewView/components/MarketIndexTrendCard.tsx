import { useEffect } from "react";
import type { SyntheticEvent } from "react";
import { Card, CardContent, Skeleton, Tab, Tabs, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { marketIndexBarsQueryOptions } from "../../../api/market";
import { MarketDataState } from "../../../components/MarketDataState";
import { MarketKLineChart } from "../../../components/MarketKLineChart";
import type { MarketOverview } from "../../../types/market";

/** 将一个交易日向前平移自然日，仅用于请求历史窗口。 */
function subtractCalendarDays(date: string, days: number): string {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() - days);
  return value.toISOString().slice(0, 10);
}

/** 渲染主要指数来源日 K 线，不以指数成分观察快照替代行情。 */
export function MarketIndexTrendCard({
  indices,
  tradeDate,
}: {
  indices: MarketOverview["indices"];
  tradeDate: string;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedIndexId = searchParams.get("index");
  const defaultIndexId = indices[0]?.indexId ?? "unavailable";
  const indexId = indices.some(
    /** 只接受当前完整包声明的固定指数身份。 */
    (index) => index.indexId === requestedIndexId,
  )
    ? (requestedIndexId ?? defaultIndexId)
    : defaultIndexId;
  const query = useQuery(
    marketIndexBarsQueryOptions({
      indexId,
      start: subtractCalendarDays(tradeDate, 370),
      end: tradeDate,
      limit: 300,
    }),
  );

  /** 删除未知或等于默认值的指数参数，保持首页图表 URL 唯一。 */
  useEffect(() => {
    if (requestedIndexId === null) return;
    if (requestedIndexId !== defaultIndexId && requestedIndexId === indexId) return;
    const next = new URLSearchParams(searchParams);
    next.delete("index");
    setSearchParams(next, { replace: true });
  }, [defaultIndexId, indexId, requestedIndexId, searchParams, setSearchParams]);

  /** 切换固定指数身份并让 Query 按独立缓存键读取真实历史。 */
  function handleIndexChange(_event: SyntheticEvent, nextIndexId: string): void {
    const next = new URLSearchParams(searchParams);
    if (nextIndexId === defaultIndexId) next.delete("index");
    else next.set("index", nextIndexId);
    setSearchParams(next);
  }

  /** 只重试当前指数图表查询。 */
  function handleRetry(): void {
    void query.refetch();
  }

  return (
    <Card component="section" aria-label="主要指数趋势">
      <CardContent>
        <Typography variant="h6">主要指数趋势</Typography>
        <Tabs
          value={indexId}
          onChange={handleIndexChange}
          aria-label="主要指数选择"
          sx={{ mt: 0.5 }}
        >
          {indices.map(
            /** 用固定指数 identity 保持图表状态和缓存隔离。 */
            (index) => (
              <Tab key={index.indexId} value={index.indexId} label={index.name} />
            ),
          )}
        </Tabs>
        <Typography variant="caption" color="text.secondary">
          成交量单位：手；来源未报告时保持空值，不进行换算
          {query.data === undefined
            ? "。"
            : `；输入 publication 版本 ${query.data.payload.inputDataVersions.length} 个。`}
        </Typography>
        {query.isPending ? (
          <Skeleton variant="rounded" height={420} sx={{ mt: 1 }} />
        ) : query.data === undefined ? (
          <MarketDataState
            variant="error"
            title="指数趋势暂不可用"
            message="真实指数日线请求失败；页面不会用指数成分数据替代。"
            onRetry={handleRetry}
            minHeight={420}
          />
        ) : query.data.payload.items.length === 0 ? (
          <MarketDataState
            variant="empty"
            title="该窗口没有指数日线"
            message="服务端返回了有效 publication，但所选日期窗口内没有记录。"
            minHeight={420}
          />
        ) : (
          <MarketKLineChart
            identity={query.data.payload.index.indexId}
            period="1d"
            bars={query.data.payload.items.map(
              /** 将合同字段投影给 KLineChart，显示转换不参与业务计算。 */
              (bar) => ({
                date: bar.tradeDate,
                open: bar.open,
                high: bar.high,
                low: bar.low,
                close: bar.close,
                volume: bar.volume ?? undefined,
                amount: bar.amountCny ?? undefined,
              }),
            )}
          />
        )}
      </CardContent>
    </Card>
  );
}
