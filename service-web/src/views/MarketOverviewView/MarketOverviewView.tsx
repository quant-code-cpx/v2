import { Alert, Box, Chip, Skeleton, Stack } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { marketOverviewQueryOptions } from "../../api/market";
import { MarketDataState } from "../../components/MarketDataState";
import { MarketPageHeader } from "../../components/MarketPageHeader";
import { formatMarketDateTime } from "../../utils/market-formatters";
import { MarketAttentionCard } from "./components/MarketAttentionCard";
import { MarketIndexGrid } from "./components/MarketIndexGrid";
import { MarketIndexTrendCard } from "./components/MarketIndexTrendCard";
import { MarketPulseGrid } from "./components/MarketPulseGrid";
import { MarketRankingCards } from "./components/MarketRankingCards";
import { MarketSectorRankingCard } from "./components/MarketSectorRankingCard";

/** 将日程派生市场状态转换为首屏可读标签。 */
function marketStateLabel(
  state: "pre_open" | "trading" | "lunch_break" | "closed" | "non_trading_day",
): string {
  if (state === "pre_open") return "盘前";
  if (state === "trading") return "交易中";
  if (state === "lunch_break") return "午间休市";
  if (state === "closed") return "已收盘";
  return "非交易日";
}

/** 将完整包新鲜度原因转换为不隐藏恢复事实的页面文案。 */
function freshnessReasonLabel(
  reason:
    | "latest_eligible_complete"
    | "latest_eligible_bundle_incomplete"
    | "latest_eligible_bundle_unavailable"
    | "publication_rollback"
    | "historical_snapshot",
): string {
  if (reason === "latest_eligible_complete") return "最新合格交易日完整包已发布";
  if (reason === "latest_eligible_bundle_incomplete") return "最新合格交易日完整包未通过质量门";
  if (reason === "latest_eligible_bundle_unavailable") return "最新合格交易日完整包尚不可用";
  if (reason === "historical_snapshot") return "历史交易日收盘快照";
  return "publication 已回滚到上一可信版本";
}

/** 判断 URL 日期是否是可传给服务端的日历日期。 */
function isDateOnly(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
}

/** 渲染真实市场完整包；首屏优先展示市场状态、指数和市场体温。 */
export function MarketOverviewView() {
  const [searchParams, setSearchParams] = useSearchParams();
  const rawAsOf = searchParams.get("asOf");
  const invalidAsOf = rawAsOf !== null && !isDateOnly(rawAsOf);
  const asOf = rawAsOf === null || invalidAsOf ? undefined : rawAsOf;
  const query = useQuery({ ...marketOverviewQueryOptions(asOf), enabled: !invalidAsOf });

  /** 发起同一完整包的条件刷新，不拆散组件 publication。 */
  function handleRefresh(): void {
    void query.refetch();
  }

  /** 清除无效日期并恢复 latest URL 状态。 */
  function handleResetDate(): void {
    setSearchParams(
      /** 仅删除当前页面拥有的日期参数，保留未来兼容参数。 */
      (current) => {
        current.delete("asOf");
        return current;
      },
      { replace: true },
    );
  }

  if (invalidAsOf) {
    return (
      <Stack spacing={3}>
        <MarketPageHeader title="市场概览" subtitle="指定交易日必须使用 YYYY-MM-DD。" />
        <MarketDataState
          variant="error"
          title="日期参数无效"
          message={`asOf=${rawAsOf ?? ""} 不是有效日历日期。`}
          onRetry={handleResetDate}
          minHeight={360}
        />
      </Stack>
    );
  }

  if (query.isPending) {
    return <MarketOverviewLoading />;
  }

  if (query.data === undefined) {
    return (
      <Stack spacing={3}>
        <MarketPageHeader title="市场概览" subtitle="市场完整包尚未成功载入。" />
        <MarketDataState
          variant="error"
          title="市场完整包不可用"
          message="API 未返回通过严格合同与质量门的数据，页面不会以空值或估算替代。"
          onRetry={handleRefresh}
          minHeight={420}
        />
      </Stack>
    );
  }

  const overview = query.data.payload;
  const isHistorical = overview.status.freshnessReason === "historical_snapshot";
  return (
    <Stack spacing={3}>
      <MarketPageHeader
        title="市场概览"
        subtitle={`${overview.tradeDate} EOD ${overview.finality} · 发布于 ${formatMarketDateTime(
          overview.publishedAt,
        )} · 非实时行情`}
        status={
          <Stack direction="row" spacing={1}>
            <Chip
              size="small"
              color={
                overview.status.marketState === "trading" ||
                overview.status.marketState === "pre_open"
                  ? "info"
                  : "default"
              }
              variant="outlined"
              label={marketStateLabel(overview.status.marketState)}
            />
            <Chip
              size="small"
              color={
                isHistorical
                  ? "info"
                  : overview.status.freshness === "current"
                    ? "success"
                    : "warning"
              }
              variant="outlined"
              label={
                isHistorical
                  ? "历史快照"
                  : overview.status.freshness === "current"
                    ? "EOD 数据及时"
                    : "EOD 数据陈旧"
              }
            />
          </Stack>
        }
        onRefresh={handleRefresh}
        refreshing={query.isFetching}
      />
      {isHistorical ? (
        <Alert severity="info">
          历史快照：{overview.tradeDate} 收盘状态，冻结时点{" "}
          {formatMarketDateTime(overview.status.marketStateAsOf)}
          ；该状态不表达当前市场或数据延迟，也不会参与 latest 轮询。当前展示 final EOD 完整包。
        </Alert>
      ) : (
        <Alert severity={overview.status.marketState === "trading" ? "info" : "success"}>
          今日市场状态：{marketStateLabel(overview.status.marketState)}，状态时点{" "}
          {formatMarketDateTime(overview.status.marketStateAsOf)}
          ；由交易日历与会话日程推导，不代表实时行情协议。当前展示 {overview.tradeDate} 的 final EOD
          完整包；EOD 资格规则 {overview.status.eodEligibilityScheduleVersion}。
        </Alert>
      )}
      {overview.status.freshness === "stale" && !isHistorical ? (
        <Alert severity="warning">
          EOD 数据滞后 {overview.status.lagTradingDays} 个交易日：最新应有交易日{" "}
          {overview.status.latestEligibleTradeDate}，最近尝试{" "}
          {overview.status.latestAttemptedTradeDate ?? "尚无可确认尝试"}；原因：
          {freshnessReasonLabel(overview.status.freshnessReason)}
          。页面保留真实旧包，所有卡片仍保持同一数据版本。
        </Alert>
      ) : null}
      {query.isRefetchError ? (
        <Alert severity="warning">
          后台刷新失败，现继续展示上一次已校验完整包；请使用页面刷新按钮重试。
        </Alert>
      ) : null}
      <MarketIndexGrid indices={overview.indices} />
      <MarketPulseGrid overview={overview} />
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.25fr) minmax(360px, 0.75fr)",
          gap: 2,
          alignItems: "start",
        }}
      >
        <MarketIndexTrendCard indices={overview.indices} tradeDate={overview.tradeDate} />
        <MarketSectorRankingCard overview={overview} />
      </Box>
      <MarketRankingCards overview={overview} />
      <MarketAttentionCard overview={overview} />
    </Stack>
  );
}

/** 查询市场完整包期间保留标题、指数与体温卡片的桌面几何。 */
function MarketOverviewLoading() {
  return (
    <Stack spacing={3}>
      <MarketPageHeader title="市场概览" subtitle="正在读取通过质量门的市场完整包。" />
      <Box sx={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 2 }}>
        {Array.from({ length: 4 }).map(
          /** 骨架仅表达布局，不填充任何业务数值。 */
          (_item, index) => (
            <Skeleton key={index} variant="rounded" height={146} />
          ),
        )}
      </Box>
      <Skeleton variant="rounded" height={170} />
      <Skeleton variant="rounded" height={440} />
    </Stack>
  );
}
