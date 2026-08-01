import { lazy, Suspense } from "react";
import {
  Alert,
  Box,
  Button,
  ButtonGroup,
  Card,
  CardContent,
  Chip,
  Divider,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";

import { isApiError } from "../../../api/http";
import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { DatasetError, DatasetLoading, DatasetUnavailable } from "./DatasetStates";

/** KLineChart 引擎与适配器只在行情页签真正渲染时加载。 */
const EquityKlineChart = lazy(async () => {
  const { EquityKlineChart: Component } = await import("./EquityKlineChart");
  return { default: Component };
});

/** 渲染真实日/周/月行情、复权控制与公司行动。 */
export function MarketTabPanel({ model }: { model: EquityDetailModel }) {
  const barsStatusFamily =
    model.state.period === "1d" ? "BARS_1D" : model.state.period === "1w" ? "BARS_1W" : "BARS_1MO";
  const barsStatus = model.status?.datasets.find(
    /** 日、周、月是三个独立物理 publication，状态不能共用。 */
    (dataset) => dataset.family === barsStatusFamily,
  );
  const publicationUnavailable =
    isApiError(model.barsQuery.error) &&
    model.barsQuery.error.status === 503 &&
    model.barsQuery.error.code === "publication-unavailable";
  const coverageUnavailable =
    isApiError(model.barsQuery.error) &&
    model.barsQuery.error.status === 409 &&
    model.barsQuery.error.code === "coverage-unavailable";
  const snapshotExpired =
    isApiError(model.barsQuery.error) &&
    model.barsQuery.error.status === 409 &&
    model.barsQuery.error.code === "snapshot-expired";
  /**
   * 当前请求已经没有 publication、精确覆盖或固定快照时，绝不回显 Query 缓存的旧窗口正文。
   * TanStack Query 会在刷新失败时保留 last-good data；继续把它交给 KLineChart 会把已失效行情
   * 误呈现为当前查询结果。
   */
  const bars =
    coverageUnavailable || publicationUnavailable || snapshotExpired ? undefined : model.bars;

  /** 切换到未复权并保留当前物理周期和日期范围。 */
  const useUnadjusted = () => model.updateState({ adjust: "none" });

  return (
    <Stack spacing={2}>
      <Card>
        <CardContent>
          <Stack
            direction="row"
            alignItems="center"
            justifyContent="space-between"
            spacing={2}
            sx={{ mb: 2 }}
          >
            <Box>
              <Typography variant="h6">价格与成交量</Typography>
              <Typography variant="body2" color="text.secondary">
                上游独立物理周期 · Asia/Shanghai · 最近合格 EOD，非实时
              </Typography>
            </Box>
            <Stack direction="row" spacing={1}>
              <ButtonGroup size="small" aria-label="K 线周期">
                {(["1d", "1w", "1mo"] as const).map((period) => (
                  <Button
                    key={period}
                    variant={model.state.period === period ? "contained" : "outlined"}
                    onClick={() => model.updateState({ period })}
                  >
                    {period === "1d" ? "日线" : period === "1w" ? "周线" : "月线"}
                  </Button>
                ))}
              </ButtonGroup>
              <ButtonGroup size="small" aria-label="复权方式">
                {(["none", "qfq", "hfq"] as const).map((adjust) => (
                  <Button
                    key={adjust}
                    variant={model.state.adjust === adjust ? "contained" : "outlined"}
                    onClick={() => model.updateState({ adjust })}
                  >
                    {adjust === "none" ? "未复权" : adjust === "qfq" ? "前复权" : "后复权"}
                  </Button>
                ))}
              </ButtonGroup>
              <ButtonGroup size="small" aria-label="历史范围">
                {(["1y", "3y", "all"] as const).map((range) => (
                  <Button
                    key={range}
                    variant={model.state.range === range ? "contained" : "outlined"}
                    onClick={() => model.updateState({ range })}
                  >
                    {range === "1y" ? "近 1 年" : range === "3y" ? "近 3 年" : "全部"}
                  </Button>
                ))}
              </ButtonGroup>
            </Stack>
          </Stack>

          {model.barsQuery.isPending ? <DatasetLoading label="正在加载 K 线行情" /> : null}
          {publicationUnavailable ? (
            <DatasetUnavailable title={`${model.state.period} 行情`} status={barsStatus} />
          ) : null}
          {coverageUnavailable ? (
            <Alert
              severity="warning"
              action={
                <Button color="inherit" size="small" onClick={() => void model.barsQuery.refetch()}>
                  重试行情
                </Button>
              }
            >
              <Typography variant="subtitle2">当前窗口尚无精确 K 线覆盖</Typography>
              <Typography variant="body2" sx={{ mt: 0.5 }}>
                coverage-unavailable · 页面不会以邻近日期、其他物理周期或 last-good
                数据替代本次请求。
              </Typography>
            </Alert>
          ) : null}
          {model.barsQuery.isError &&
          !model.barsQuery.isFetchNextPageError &&
          !publicationUnavailable &&
          !coverageUnavailable ? (
            <Stack spacing={1}>
              <DatasetError
                title="K 线行情"
                error={model.barsQuery.error}
                retry={() => void model.barsQuery.refetch()}
              />
              {isApiError(model.barsQuery.error) &&
              model.barsQuery.error.status === 409 &&
              model.barsQuery.error.code === "adjustment-unavailable" ? (
                <Button variant="outlined" onClick={useUnadjusted} sx={{ alignSelf: "flex-start" }}>
                  明确切换为未复权
                </Button>
              ) : null}
            </Stack>
          ) : null}
          {model.barsQuery.isFetchingNextPage ? (
            <Alert severity="info" sx={{ mb: 1 }}>
              正在沿同一 publication 的签名 cursor 加载完整历史；已校验 {bars?.items.length ?? 0}{" "}
              条。
            </Alert>
          ) : null}
          {model.barsQuery.isFetchNextPageError ? (
            <DatasetError
              title="更早 K 线分页"
              error={model.barsQuery.error}
              retry={() => void model.barsQuery.fetchNextPage()}
            />
          ) : null}
          {bars !== undefined &&
          bars.nextCursor !== null &&
          !model.barsQuery.hasNextPage &&
          !model.barsQuery.isFetchingNextPage ? (
            <Alert severity="warning" sx={{ mb: 1 }}>
              当前窗口超过 16,000 条浏览器缓存预算；请改用近 1 年或近 3 年窗口。
            </Alert>
          ) : null}
          {bars?.publicationKind === "ZERO_RECORD_COVERAGE" ? (
            <Alert severity="info">
              当前物理周期和日期窗口已有精确零记录覆盖；它不是
              NO_PUBLICATION，也没有用零值生成行情。
            </Alert>
          ) : null}
          {bars?.availability === "SOURCE_UNAVAILABLE" ? (
            <Alert severity="warning" sx={{ mb: 1 }}>
              行情来源本次同步不可用（{bars.reasonCode ?? "SOURCE_UNAVAILABLE"}）； 如下数据仅为
              last-good publication。
            </Alert>
          ) : null}
          {bars?.stale ? (
            <Alert severity="info" sx={{ mb: 1 }}>
              新发布检查失败，当前 K 线保留最后合格版本，只读展示。
            </Alert>
          ) : null}
          {bars !== undefined && bars.items.length > 0 ? (
            <Suspense fallback={<Skeleton variant="rounded" height={480} />}>
              <EquityKlineChart
                exchange={model.exchange ?? ""}
                symbol={model.symbol}
                period={model.state.period}
                page={bars}
              />
            </Suspense>
          ) : null}
          {bars !== undefined ? (
            <Stack spacing={0.5} sx={{ mt: 1.5 }} role="note" aria-label="K 线数据说明">
              <Typography variant="caption" color="text.secondary">
                数据说明 ·{bars.publicationKind === "DATA" ? " 数据精确覆盖" : " 零记录精确覆盖"}
              </Typography>
              <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", rowGap: 1 }}>
                <Chip size="small" label={`dataVersion ${bars.dataVersion}`} />
                <Chip size="small" label={`publishedAt ${bars.publishedAt}`} />
                <Chip
                  size="small"
                  variant="outlined"
                  label={`coverageVersion ${bars.coverageVersion}`}
                />
                <Chip
                  size="small"
                  variant="outlined"
                  label={`publicationKind ${bars.publicationKind}`}
                />
                <Chip
                  size="small"
                  variant="outlined"
                  label={`sourceBatchId ${bars.sourceBatchId}`}
                />
                <Chip
                  size="small"
                  label={
                    model.state.adjust === "none"
                      ? "未复权"
                      : `${model.state.adjust} · adjustAsOf ${bars.adjustAsOf ?? "—"}`
                  }
                />
                {model.state.adjust === "none" ? null : (
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`factorVersion ${bars.factorVersion ?? "—"} · formula v${bars.formulaVersion ?? "—"}`}
                  />
                )}
              </Stack>
            </Stack>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Typography variant="h6">公司行动</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
            分红、送股和转增来自独立 publication；失败不影响 K 线本身。
          </Typography>
          {model.statusQuery.isPending ? <Skeleton variant="rounded" height={96} /> : null}
          {model.statusQuery.isError ? (
            <DatasetError
              title="公司行动状态"
              error={model.statusQuery.error}
              retry={() => void model.statusQuery.refetch()}
            />
          ) : null}
          {model.statusQuery.isSuccess &&
          model.corporateActionStatus?.availability !== "AVAILABLE" ? (
            <DatasetUnavailable title="公司行动" status={model.corporateActionStatus} />
          ) : null}
          {model.corporateActionStatus?.freshness === "STALE" ? (
            <Alert severity="info">
              公司行动正在展示 last-good publication：
              {model.corporateActionStatus.dataVersion ?? "版本未知"}。
            </Alert>
          ) : null}
          {model.corporateActionStatus?.availability === "AVAILABLE" &&
          model.corporateActionsQuery.isPending ? (
            <Skeleton variant="rounded" height={96} />
          ) : null}
          {model.corporateActionsQuery.isError ? (
            <DatasetError
              title="公司行动"
              error={model.corporateActionsQuery.error}
              retry={() => void model.corporateActionsQuery.refetch()}
            />
          ) : null}
          {model.corporateActions?.items.length === 0 ? (
            <Alert severity="info">该日期窗口没有已发布公司行动。</Alert>
          ) : null}
          <Stack
            divider={<Divider flexItem />}
            spacing={1.5}
            sx={{ maxHeight: 420, overflowY: "auto", pr: 1 }}
          >
            {/* 每条公司行动保留报告期、除权日和每十股口径。 */}
            {model.corporateActions?.items.map((action) => (
              <Stack key={action.actionId} direction="row" justifyContent="space-between">
                <Box>
                  <Typography variant="subtitle2">
                    {action.reportPeriod} · {action.status}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    公告 {action.announcementDate ?? "—"} · 登记 {action.recordDate ?? "—"} · 除权{" "}
                    {action.exDate ?? "—"}
                  </Typography>
                </Box>
                <Typography variant="body2">
                  每 10 股：现金 {action.cashDividendPer10 ?? "—"} / 送股{" "}
                  {action.bonusSharesPer10 ?? "—"} / 转增 {action.transferSharesPer10 ?? "—"}
                </Typography>
              </Stack>
            ))}
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
