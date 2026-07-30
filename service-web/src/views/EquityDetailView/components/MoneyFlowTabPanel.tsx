import { lazy, Suspense } from "react";
import { Alert, Card, CardContent, Chip, Skeleton, Stack, Typography } from "@mui/material";

import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { DatasetError, DatasetStaleNotice, DatasetUnavailable } from "./DatasetStates";

/** ECharts 只在资金流页签出现真实日频序列时加载。 */
const MoneyFlowChart = lazy(async () => {
  const { MoneyFlowChart: Component } = await import("./MoneyFlowChart");
  return { default: Component };
});

/** 渲染供应商方法学、分桶、分母与日频资金流。 */
export function MoneyFlowTabPanel({ model }: { model: EquityDetailModel }) {
  if (model.statusQuery.isPending) {
    return <Skeleton variant="rounded" height={420} aria-label="正在读取资金流数据状态" />;
  }
  if (model.statusQuery.isError) {
    return (
      <DatasetError
        title="资金流数据状态"
        error={model.statusQuery.error}
        retry={() => void model.statusQuery.refetch()}
      />
    );
  }

  return (
    <Stack spacing={2}>
      {model.statusQuery.isSuccess && model.moneyFlowStatus?.availability !== "AVAILABLE" ? (
        <DatasetUnavailable title="日频资金流" status={model.moneyFlowStatus} />
      ) : null}
      {model.moneyFlowStatus?.freshness === "STALE" ? (
        <DatasetStaleNotice status={model.moneyFlowStatus} />
      ) : null}
      {model.moneyFlowQuery.isFetching && model.moneyFlow === undefined ? (
        <Skeleton variant="rounded" height={420} aria-label="正在加载日频资金流" />
      ) : null}
      {model.moneyFlowQuery.isError ? (
        <DatasetError
          title="日频资金流"
          error={model.moneyFlowQuery.error}
          retry={() => void model.moneyFlowQuery.refetch()}
        />
      ) : null}
      {model.moneyFlow?.items.length === 0 ? (
        <Alert severity="info">当前方法学、分桶和日期窗口没有资金流观察。</Alert>
      ) : null}
      {model.moneyFlow !== undefined && model.moneyFlow.items.length > 0 ? (
        <Card>
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
              <div>
                <Typography variant="h6">主力资金净流入 / 净流出</Typography>
                <Typography variant="body2" color="text.secondary">
                  {model.moneyFlow.upstreamSource} · {model.moneyFlow.sourceDataset} · 查询窗口{" "}
                  {model.moneyFlowWindow.start} 至 {model.moneyFlowWindow.end}
                </Typography>
              </div>
              <Stack direction="row" spacing={1}>
                <Chip
                  label={`${model.moneyFlow.methodologyId} v${model.moneyFlow.methodologyVersion}`}
                />
                <Chip label={`bucket ${model.moneyFlow.bucket}`} variant="outlined" />
                <Chip
                  label={`${model.moneyFlow.windowType} · ${model.moneyFlow.windowSize} 日`}
                  variant="outlined"
                />
              </Stack>
            </Stack>
            <Suspense fallback={<Skeleton variant="rounded" height={360} sx={{ mt: 2 }} />}>
              <MoneyFlowChart page={model.moneyFlow} />
            </Suspense>
            <Stack spacing={0.5} sx={{ mt: 1 }}>
              <Typography variant="caption" color="text.secondary">
                净额方向：{model.moneyFlow.directionDefinition}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                比率分母：{model.moneyFlow.ratioDenominator}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                dataVersion {model.moneyFlow.dataVersion} · publishedAt{" "}
                {model.moneyFlow.publishedAt}
              </Typography>
            </Stack>
          </CardContent>
        </Card>
      ) : null}
    </Stack>
  );
}
