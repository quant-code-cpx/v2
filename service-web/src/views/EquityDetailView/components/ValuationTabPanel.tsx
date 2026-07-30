import { lazy, Suspense } from "react";
import { Alert, Card, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";

import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { DatasetError, DatasetStaleNotice, DatasetUnavailable } from "./DatasetStates";

/** ECharts 只在估值页签出现真实观察值时加载。 */
const ValuationChart = lazy(async () => {
  const { ValuationChart: Component } = await import("./ValuationChart");
  return { default: Component };
});

/** 渲染显式供应商方法学的历史估值观察。 */
export function ValuationTabPanel({ model }: { model: EquityDetailModel }) {
  if (model.statusQuery.isPending) {
    return <Skeleton variant="rounded" height={420} aria-label="正在读取估值数据状态" />;
  }
  if (model.statusQuery.isError) {
    return (
      <DatasetError
        title="估值数据状态"
        error={model.statusQuery.error}
        retry={() => void model.statusQuery.refetch()}
      />
    );
  }

  return (
    <Stack spacing={2}>
      {model.statusQuery.isSuccess && model.valuationStatus?.availability !== "AVAILABLE" ? (
        <DatasetUnavailable title="历史估值" status={model.valuationStatus} />
      ) : null}
      {model.valuationStatus?.freshness === "STALE" ? (
        <DatasetStaleNotice status={model.valuationStatus} />
      ) : null}
      {model.valuationQuery.isFetching && model.valuations === undefined ? (
        <Skeleton variant="rounded" height={420} aria-label="正在加载历史估值" />
      ) : null}
      {model.valuationQuery.isError ? (
        <DatasetError
          title="历史估值"
          error={model.valuationQuery.error}
          retry={() => void model.valuationQuery.refetch()}
        />
      ) : null}
      {model.valuations?.items.length === 0 ? (
        <Alert severity="info">当前方法学和日期窗口没有估值观察。</Alert>
      ) : null}
      {model.valuations !== undefined && model.valuations.items.length > 0 ? (
        <Card>
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
              <div>
                <Typography variant="h6">历史 PE TTM</Typography>
                <Typography variant="body2" color="text.secondary">
                  供应商观察，不是官方最终值；不跨方法学补值。查询窗口 {model.valuationWindow.start}{" "}
                  至 {model.valuationWindow.end}。
                </Typography>
              </div>
              <Stack direction="row" spacing={1}>
                <Chip label={model.valuations.methodologyCode} />
                <Chip label={`v${model.valuations.methodologyVersion}`} variant="outlined" />
              </Stack>
            </Stack>
            <Suspense fallback={<Skeleton variant="rounded" height={360} sx={{ mt: 2 }} />}>
              <ValuationChart page={model.valuations} />
            </Suspense>
            <Typography variant="caption" color="text.secondary">
              dataVersion {model.valuations.dataVersion} · publishedAt{" "}
              {model.valuations.publishedAt} · finality PROVIDER_OBSERVATION
            </Typography>
          </CardContent>
        </Card>
      ) : null}
    </Stack>
  );
}
