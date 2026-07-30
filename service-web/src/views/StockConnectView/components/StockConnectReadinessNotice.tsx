import { Alert, Button, Chip, Stack, Typography } from "@mui/material";

import { isApiError } from "../../../api/http";
import type {
  StockConnectReadinessReasonCode,
  StockConnectReadinessResponse,
  StockConnectReadinessState,
} from "../../../types/stock-connect";
import { stockConnectChannelLabel } from "../utils/stock-connect-presentation";

/** 冻结 readiness 原因的中文公开说明，不展示内部异常、路径或凭证状态原文。 */
const readinessReasonLabels: Record<StockConnectReadinessReasonCode, string> = {
  BUNDLE_PUBLISHED: "bundle 已发布",
  OFFICIAL_CALENDAR_CLOSED: "官方日历闭市",
  CALENDAR_EVIDENCE_MISSING: "缺少日历证据",
  CALENDAR_SOURCE_MISSING: "日历来源缺失",
  DELIVERY_ENTITLEMENT_MISSING: "交付授权缺失",
  DELIVERY_OBJECT_MISSING: "交付对象缺失",
  STATUS_SOURCE_MISSING: "通道状态来源缺失",
  PREFLIGHT_PENDING: "预检进行中",
  PREFLIGHT_FAILED: "预检失败",
  COMMAND_NOT_SUBMITTED: "同步命令尚未提交",
  EXECUTION_PENDING: "同步执行中",
  EXECUTION_SOURCE_MISSING: "执行所需来源缺失",
  EXECUTION_FAILED: "同步执行失败",
  PUBLICATION_INCOMPLETE: "publication 组件未齐备",
};

/** 描述 readiness 提示需要的独立远程状态。 */
interface StockConnectReadinessNoticeProps {
  readiness: StockConnectReadinessResponse | undefined;
  isPending: boolean;
  error: unknown;
  onRetry: () => void;
}

/** 把候选日、共同 ready 日与逐通道证据状态展示为紧凑且可读的桌面提示。 */
export function StockConnectReadinessNotice({
  readiness,
  isPending,
  error,
  onRetry,
}: StockConnectReadinessNoticeProps) {
  if (readiness === undefined && isPending) {
    return <Alert severity="info">正在读取候选交易日与逐通道 readiness 证据…</Alert>;
  }
  if (readiness === undefined && error !== null) {
    const notObserved = isApiError(error) && error.code === "READINESS_NOT_OBSERVED";
    return (
      <Alert
        severity="warning"
        action={
          <Button color="inherit" size="small" onClick={onRetry}>
            重试
          </Button>
        }
      >
        {notObserved
          ? "尚无与当前筛选匹配的持久化 readiness 快照；页面不会根据当前时间猜测休市或失败原因。"
          : "readiness 证据暂不可读；已发布业务数据仍保持原 publication，不会与同步状态拼接。"}
      </Alert>
    );
  }
  if (readiness === undefined) return null;

  const severity = readinessSeverity(readiness);
  return (
    <Alert severity={severity}>
      <Stack spacing={1}>
        <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap">
          <Typography variant="body2" fontWeight={600}>
            候选交易日：{readiness.candidateTradeDate ?? "未形成"}
          </Typography>
          <Typography variant="body2">
            共同已发布日：{readiness.readyTradeDate ?? "暂无"}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            证据截至 {readiness.observedAt}
          </Typography>
        </Stack>
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {readiness.channels.map(
            /** 每条通道独立显示状态与固定原因，绝不由其他通道状态推断。 */
            (item) => (
              <Chip
                key={item.channel}
                size="small"
                color={readinessStateColor(item.state)}
                label={`${stockConnectChannelLabel(item.channel)} · ${readinessStateLabel(item.state)} · ${
                  readinessReasonLabels[item.reasonCode]
                }`}
              />
            ),
          )}
        </Stack>
      </Stack>
    </Alert>
  );
}

/** 根据最严重的逐通道状态选择不依赖颜色才能理解的 Alert 级别。 */
function readinessSeverity(
  readiness: StockConnectReadinessResponse,
): "success" | "info" | "warning" | "error" {
  if (readiness.channels.some((item) => item.state === "FAILED")) return "error";
  if (readiness.channels.some((item) => item.state === "SOURCE_MISSING")) return "warning";
  if (readiness.channels.some((item) => item.state === "PENDING")) return "info";
  if (readiness.channels.some((item) => item.state === "NOT_TRADING")) return "info";
  return "success";
}

/** 返回 readiness 状态的固定中文文本。 */
function readinessStateLabel(state: StockConnectReadinessState): string {
  const labels: Record<StockConnectReadinessState, string> = {
    READY: "已就绪",
    PENDING: "等待中",
    FAILED: "失败",
    SOURCE_MISSING: "来源缺失",
    NOT_TRADING: "非交易日",
  };
  return labels[state];
}

/** 返回 Chip 的语义色；标签文字仍是状态的主要载体。 */
function readinessStateColor(
  state: StockConnectReadinessState,
): "success" | "info" | "warning" | "error" | "default" {
  const colors: Record<
    StockConnectReadinessState,
    "success" | "info" | "warning" | "error" | "default"
  > = {
    READY: "success",
    PENDING: "info",
    FAILED: "error",
    SOURCE_MISSING: "warning",
    NOT_TRADING: "default",
  };
  return colors[state];
}
