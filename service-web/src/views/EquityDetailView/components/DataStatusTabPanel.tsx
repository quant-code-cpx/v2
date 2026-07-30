import {
  Alert,
  Button,
  Card,
  CardContent,
  Chip,
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

import type { EquityAvailability, EquityDatasetStatus } from "../../../types/equity-market";
import type { EquityDetailModel } from "../hooks/useEquityDetail";
import { equityDetailDatasetFamilies } from "../hooks/useEquityDetail";
import { DatasetError } from "./DatasetStates";

/** 返回数据集 family 的稳定中文短标签。 */
function datasetFamilyLabel(family: string): string {
  const labels: Record<string, string> = {
    IDENTITY: "证券身份",
    COMPANY_PROFILE: "公司概况",
    BARS_1D: "日线",
    BARS_1W: "周线",
    BARS_1MO: "月线",
    ADJUSTMENT_FACTOR: "复权因子",
    CORPORATE_ACTION: "公司行动",
    FINANCIAL_REPORT: "财务报告",
    FINANCIAL_INDICATOR: "财务指标",
    VALUATION: "历史估值",
    MONEY_FLOW: "个股资金流",
    INDUSTRY_MEMBERSHIP: "行业归属",
    CONCEPT_MEMBERSHIP: "概念归属",
    SW_INDUSTRY_MEMBERSHIP: "申万行业",
    EARNINGS_FORECAST: "业绩预告",
    EARNINGS_EXPRESS: "业绩快报",
    DRAGON_TIGER: "龙虎榜",
    BLOCK_TRADE: "大宗交易",
  };
  return labels[family] ?? family;
}

/** 把数据可用性映射为文字和 MUI 语义色，避免只靠颜色传达状态。 */
function availabilityPresentation(availability: EquityAvailability): {
  label: string;
  color: "success" | "warning" | "error" | "default";
} {
  if (availability === "AVAILABLE") return { label: "可用", color: "success" };
  if (availability === "PARTIAL") return { label: "部分可用", color: "warning" };
  if (availability === "EMPTY") return { label: "合法空数据", color: "default" };
  if (availability === "SOURCE_UNAVAILABLE") {
    return { label: "来源不可用", color: "error" };
  }
  return { label: "无 publication", color: "error" };
}

/** 渲染一条数据集状态，明确 publication、来源、方法学与恢复语义。 */
function DatasetStatusRow({ status }: { status: EquityDatasetStatus }) {
  const availability = availabilityPresentation(status.availability);
  return (
    <TableRow hover>
      <TableCell>
        <Typography variant="subtitle2">{datasetFamilyLabel(status.family)}</Typography>
        <Typography variant="caption" color="text.secondary">
          {status.family}
        </Typography>
      </TableCell>
      <TableCell>
        <Stack direction="row" spacing={0.75}>
          <Chip size="small" color={availability.color} label={availability.label} />
          <Chip
            size="small"
            variant="outlined"
            color={status.freshness === "STALE" ? "warning" : "default"}
            label={
              status.freshness === "FRESH"
                ? "新鲜"
                : status.freshness === "STALE"
                  ? "陈旧"
                  : "新鲜度未知"
            }
          />
        </Stack>
      </TableCell>
      <TableCell>
        <Typography variant="body2">{status.sourceLabel ?? "—"}</Typography>
        <Typography variant="caption" color="text.secondary">
          {status.methodology === null || status.methodology === undefined
            ? "方法学未提供"
            : `${status.methodology.code} v${status.methodology.version}`}
        </Typography>
      </TableCell>
      <TableCell>
        <Typography variant="body2">{status.effectiveAsOf ?? "—"}</Typography>
        <Typography variant="caption" color="text.secondary">
          {status.publishedAt ?? "未发布"}
        </Typography>
      </TableCell>
      <TableCell sx={{ maxWidth: 260 }}>
        <Typography variant="caption" sx={{ overflowWrap: "anywhere" }}>
          {status.dataVersion ?? "—"}
        </Typography>
      </TableCell>
      <TableCell>
        <Typography variant="body2">
          {status.reasonCode ?? (status.retryable ? "可重试" : "—")}
        </Typography>
        {status.retryable ? (
          <Typography variant="caption" color="warning.main">
            允许稍后重试
          </Typography>
        ) : null}
      </TableCell>
    </TableRow>
  );
}

/** 渲染详情全部数据集的独立 availability、freshness 与 publication 状态。 */
export function DataStatusTabPanel({ model }: { model: EquityDetailModel }) {
  if (model.statusQuery.isPending) {
    return <Skeleton variant="rounded" height={520} aria-label="正在加载数据状态" />;
  }

  if (model.statusQuery.isError) {
    return (
      <DatasetError
        title="数据状态"
        error={model.statusQuery.error}
        retry={() => void model.statusQuery.refetch()}
      />
    );
  }

  if (model.status === undefined || model.status.datasets.length === 0) {
    return (
      <Alert severity="warning">
        数据状态端点未返回任何 family；事实页签不会将该状态解释为“可用”。
      </Alert>
    );
  }

  const returnedFamilies = new Set(
    model.status.datasets.map(
      /** family 是公开稳定数据集身份。 */
      (status) => status.family,
    ),
  );
  const missingFamilies = equityDetailDatasetFamilies.filter(
    /** 请求过但未返回的 family 必须显式暴露，不能当作 AVAILABLE。 */
    (family) => !returnedFamilies.has(family),
  );

  return (
    <Card>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
          <div>
            <Typography variant="h6">数据状态与血缘</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              每个数据集独立发布、独立失败；状态表不包含事实记录，也不会触发数据同步。
            </Typography>
          </div>
          <Button variant="outlined" onClick={() => void model.statusQuery.refetch()}>
            刷新状态
          </Button>
        </Stack>
        {missingFamilies.length > 0 ? (
          <Alert severity="error" sx={{ mt: 2 }}>
            数据状态响应缺少 {missingFamilies.length} 个已请求 family：
            {missingFamilies.join("、")}。对应页签保持不可用，不能继续猜测。
          </Alert>
        ) : null}
        <TableContainer sx={{ mt: 2, maxHeight: 590 }}>
          <Table stickyHeader size="small" aria-label="个股数据集状态">
            <TableHead>
              <TableRow>
                <TableCell>数据集</TableCell>
                <TableCell>状态</TableCell>
                <TableCell>来源 / 方法学</TableCell>
                <TableCell>有效日 / 发布时间</TableCell>
                <TableCell>dataVersion</TableCell>
                <TableCell>原因 / 恢复</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {/* 服务端返回顺序与请求 family 对齐，便于识别真正缺失的数据集。 */}
              {model.status.datasets.map((status) => (
                <DatasetStatusRow key={`${status.family}:${status.dataset}`} status={status} />
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      </CardContent>
    </Card>
  );
}
