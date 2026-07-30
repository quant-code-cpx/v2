import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Skeleton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { useInfiniteQuery } from "@tanstack/react-query";

import { equityFinancialReportDetailInfiniteQueryOptions } from "../../../api/equity-market";
import { isApiError } from "../../../api/http";
import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { DatasetError, DatasetStaleNotice, DatasetUnavailable } from "./DatasetStates";

/** 返回财务报表类型的中文短标签。 */
function statementTypeLabel(
  value: "BALANCE_SHEET" | "INCOME_STATEMENT" | "CASH_FLOW_STATEMENT",
): string {
  if (value === "BALANCE_SHEET") return "资产负债表";
  if (value === "INCOME_STATEMENT") return "利润表";
  return "现金流量表";
}

/** 渲染真实报告列表、治理行项目和平台衍生指标。 */
export function FinancialTabPanel({ model }: { model: EquityDetailModel }) {
  const [selectedReportRef, setSelectedReportRef] = useState<string | null>(null);
  const effectiveReportRef =
    model.financialReports?.items.some((report) => report.reportRef === selectedReportRef) === true
      ? selectedReportRef
      : (model.financialReports?.items[0]?.reportRef ?? null);
  const reportDetailQuery = useInfiniteQuery({
    ...equityFinancialReportDetailInfiniteQueryOptions(
      model.exchange ?? "SSE",
      model.symbol,
      effectiveReportRef ?? "00000000-0000-0000-0000-000000000000",
      model.financialReports?.dataVersion ?? "00000000-0000-0000-0000-000000000000",
      model.resolvedIdentityAsOf,
    ),
    enabled: effectiveReportRef !== null && model.financialReports !== undefined,
  });
  const reportDetail = reportDetailQuery.data;
  const reportRecoveryRef = useRef<string | undefined>(undefined);

  // 报表行项目遇到 publication 切换时只刷新一次 status；上层列表会随新版本重建详情 query key。
  useEffect(() => {
    const version = model.financialReports?.dataVersion;
    const error = reportDetailQuery.error;
    if (
      version === undefined ||
      reportRecoveryRef.current === version ||
      !isApiError(error) ||
      error.status !== 409 ||
      error.code !== "snapshot-expired"
    ) {
      return;
    }
    reportRecoveryRef.current = version;
    void model.statusQuery.refetch();
  }, [model.financialReports?.dataVersion, model.statusQuery.refetch, reportDetailQuery.error]);

  // 当前报告的治理行项目必须沿同一 publication 取尽，不能静默丢弃后续 cursor 页。
  useEffect(() => {
    if (
      effectiveReportRef !== null &&
      reportDetailQuery.hasNextPage &&
      !reportDetailQuery.isFetchingNextPage &&
      !reportDetailQuery.isFetchNextPageError
    ) {
      void reportDetailQuery.fetchNextPage();
    }
  }, [
    effectiveReportRef,
    reportDetailQuery.fetchNextPage,
    reportDetailQuery.hasNextPage,
    reportDetailQuery.isFetchNextPageError,
    reportDetailQuery.isFetchingNextPage,
  ]);

  if (model.statusQuery.isPending) {
    return <Skeleton variant="rounded" height={420} aria-label="正在读取财务数据状态" />;
  }
  if (model.statusQuery.isError) {
    return (
      <DatasetError
        title="财务数据状态"
        error={model.statusQuery.error}
        retry={() => void model.statusQuery.refetch()}
      />
    );
  }

  return (
    <Stack spacing={2}>
      {model.statusQuery.isSuccess && model.financialStatus?.availability !== "AVAILABLE" ? (
        <DatasetUnavailable title="财务报告" status={model.financialStatus} />
      ) : null}
      {model.financialStatus?.freshness === "STALE" ? (
        <DatasetStaleNotice status={model.financialStatus} />
      ) : null}
      {model.financialQuery.isFetching && model.financialReports === undefined ? (
        <Skeleton variant="rounded" height={360} aria-label="正在加载财务报告" />
      ) : null}
      {model.financialQuery.isError ? (
        <DatasetError
          title="财务报告"
          error={model.financialQuery.error}
          retry={() => void model.financialQuery.refetch()}
        />
      ) : null}
      {model.financialReports?.items.length === 0 ? (
        <Alert severity="info">当前方法学 publication 没有符合条件的财务报告。</Alert>
      ) : null}

      {model.financialReports !== undefined && model.financialReports.items.length > 0 ? (
        <Box sx={{ display: "grid", gridTemplateColumns: "320px minmax(0, 1fr)", gap: 2 }}>
          <Card>
            <CardContent>
              <Typography variant="h6">报告期</Typography>
              <Typography variant="caption" color="text.secondary">
                {model.financialReports.methodologyCode} v
                {model.financialReports.methodologyVersion}
              </Typography>
              <Stack divider={<Divider flexItem />} sx={{ mt: 1.5 }}>
                {/* 报告选择只影响当前页签局部状态，远程实体继续由 Query 管理。 */}
                {model.financialReports.items.map((report) => (
                  <Button
                    key={report.reportRef}
                    variant={effectiveReportRef === report.reportRef ? "contained" : "text"}
                    color={effectiveReportRef === report.reportRef ? "primary" : "inherit"}
                    onClick={() => setSelectedReportRef(report.reportRef)}
                    sx={{ justifyContent: "space-between", py: 1.25 }}
                  >
                    <span>{report.reportPeriod}</span>
                    <span>{statementTypeLabel(report.statementType)}</span>
                  </Button>
                ))}
              </Stack>
            </CardContent>
          </Card>

          <Card>
            <CardContent>
              <Typography variant="h6">报表行项目</Typography>
              {reportDetailQuery.isPending ? (
                <Skeleton variant="rounded" height={280} sx={{ mt: 2 }} />
              ) : null}
              {reportDetailQuery.isError ? (
                <DatasetError
                  title="报表行项目"
                  error={reportDetailQuery.error}
                  retry={() => void reportDetailQuery.refetch()}
                />
              ) : null}
              {reportDetail !== undefined ? (
                <>
                  <Stack direction="row" spacing={1} sx={{ my: 1.5 }}>
                    <Chip label={statementTypeLabel(reportDetail.report.statementType)} />
                    <Chip label={reportDetail.report.periodBasis} variant="outlined" />
                    <Chip label={reportDetail.report.statementScope} variant="outlined" />
                    <Chip label={reportDetail.report.auditStatus} variant="outlined" />
                  </Stack>
                  <TableContainer sx={{ maxHeight: 440 }}>
                    <Table stickyHeader size="small" aria-label="财务报表行项目">
                      <TableHead>
                        <TableRow>
                          <TableCell>项目</TableCell>
                          <TableCell>代码</TableCell>
                          <TableCell align="right">值</TableCell>
                          <TableCell>单位 / 币种</TableCell>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {/* 精确 decimal string 直接展示，nullReason 不替换为零。 */}
                        {reportDetail.items.map((item) => (
                          <TableRow key={item.metricCode}>
                            <TableCell>{item.label}</TableCell>
                            <TableCell>
                              <Typography variant="caption">{item.metricCode}</Typography>
                            </TableCell>
                            <TableCell align="right">
                              {item.value ?? `—（${item.nullReason ?? "UNKNOWN"}）`}
                            </TableCell>
                            <TableCell>
                              {item.unit} · {item.currency ?? item.currencyNullReason ?? "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableContainer>
                  <Typography variant="caption" color="text.secondary">
                    dataVersion {reportDetail.dataVersion} · knowledgeCutoff{" "}
                    {reportDetail.knowledgeCutoff}
                  </Typography>
                </>
              ) : null}
            </CardContent>
          </Card>
        </Box>
      ) : null}

      <Card>
        <CardContent>
          <Typography variant="h6">平台衍生财务指标</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
            只读取 OpenAPI 已冻结的 platform.financial-derivation 指标，展示 formulaVersion 和独立
            dataVersion。
          </Typography>
          {model.statusQuery.isSuccess &&
          model.financialMetricStatus?.availability !== "AVAILABLE" ? (
            <DatasetUnavailable title="财务指标" status={model.financialMetricStatus} />
          ) : null}
          {model.financialMetricStatus?.availability === "AVAILABLE" &&
          model.financialMetricStatus.methodology?.code !== "platform.financial-derivation" ? (
            <Alert severity="info">
              当前已发布方法学为 {model.financialMetricStatus.methodology?.code ?? "未知"}；
              它不是平台衍生合同，本卡不跨方法学混读。
            </Alert>
          ) : null}
          {model.financialMetricsQuery.isFetching && model.financialMetrics === undefined ? (
            <Skeleton variant="rounded" height={140} />
          ) : null}
          {model.financialMetricsQuery.isError ? (
            <DatasetError
              title="平台衍生财务指标"
              error={model.financialMetricsQuery.error}
              retry={() => void model.financialMetricsQuery.refetch()}
            />
          ) : null}
          {model.financialMetrics !== undefined ? (
            <Stack divider={<Divider flexItem />}>
              {/* 平台派生指标按报告期与公式版本显式展示。 */}
              {model.financialMetrics.items.slice(-12).map((metric) => (
                <Stack
                  key={`${metric.metricCode}:${metric.reportPeriod}`}
                  direction="row"
                  justifyContent="space-between"
                  sx={{ py: 1.25 }}
                >
                  <Box>
                    <Typography variant="subtitle2">{metric.label}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {metric.metricCode} · {metric.reportPeriod} · {metric.periodBasis}
                    </Typography>
                  </Box>
                  <Typography align="right">
                    {metric.value} {metric.unit}
                    <Typography component="span" variant="caption" color="text.secondary">
                      {" "}
                      · formula v{metric.formulaVersion ?? "—"}
                    </Typography>
                  </Typography>
                </Stack>
              ))}
            </Stack>
          ) : null}
        </CardContent>
      </Card>
    </Stack>
  );
}
