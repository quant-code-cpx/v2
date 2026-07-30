import type { ChipProps } from "@mui/material";

import type {
  ActorDisplay,
  CommandStatus,
  DeliveryStatus,
  ErrorSummary,
  ExecutionSlotState,
  FreshnessStatus,
  HealthCheckStatus,
  HealthStatus,
  OperationResult,
  RunStatus,
} from "../../../types/data-operations";

/** 数据运维页面稳定的五个任务型工作区。 */
export const dataOperationsTabs = [
  "datasets",
  "runs",
  "health",
  "schedules",
  "operations",
] as const;

/** 表示数据运维页面可写入 URL 的有效 Tab。 */
export type DataOperationsTab = (typeof dataOperationsTabs)[number];

/** 以固定中国时区格式化服务端时刻，不参与任何 freshness 推导。 */
export function formatDataOperationsDateTime(value: string | null): string {
  if (value === null) {
    return "—";
  }

  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

/** 以固定中国时区格式化服务端日期字符串。 */
export function formatDataOperationsDate(value: string | null): string {
  return value === null ? "—" : value;
}

/** 将运行权威状态映射为明确中文文案。 */
export function runStatusLabel(status: RunStatus): string {
  const labels: Record<RunStatus, string> = {
    QUEUED: "已排队",
    RUNNING: "运行中",
    CANCEL_REQUESTED: "取消处理中",
    SUCCEEDED: "成功",
    PARTIAL: "部分成功",
    FAILED: "失败",
    CANCELLED: "已取消",
    INTERRUPTED: "已中断",
    SKIPPED: "已跳过",
  };

  return labels[status];
}

/** 将命令聚合状态映射为明确中文文案。 */
export function commandStatusLabel(status: CommandStatus): string {
  const labels: Record<CommandStatus, string> = {
    QUEUED: "已排队",
    RUNNING: "运行中",
    CANCEL_REQUESTED: "取消处理中",
    SUCCEEDED: "成功",
    PARTIAL: "部分成功",
    FAILED: "失败",
    CANCELLED: "已取消",
    REJECTED: "已拒绝",
  };

  return labels[status];
}

/** 将发布后健康状态映射为不依赖颜色的中文文案。 */
export function healthStatusLabel(status: HealthStatus): string {
  const labels: Record<HealthStatus, string> = {
    HEALTHY: "健康",
    WARN: "警告",
    CRITICAL: "严重",
    UNKNOWN: "未知",
  };

  return labels[status];
}

/** 将服务端 freshness 状态映射为直接展示文案。 */
export function freshnessStatusLabel(status: FreshnessStatus): string {
  const labels: Record<FreshnessStatus, string> = {
    FRESH: "新鲜",
    WARNING: "新鲜度警告",
    STALE: "已过期",
    UNKNOWN: "新鲜度未知",
    NOT_APPLICABLE: "新鲜度不适用",
  };

  return labels[status];
}

/** 将本地投递状态映射为不会误称下游成功的文案。 */
export function deliveryStatusLabel(status: DeliveryStatus): string {
  const labels: Record<DeliveryStatus, string> = {
    PENDING: "等待同步服务受理",
    DELIVERING: "正在投递",
    ACCEPTED: "同步服务已受理",
    REJECTED: "同步服务已拒绝",
    DEAD_LETTER: "投递失败待处理",
  };

  return labels[status];
}

/** 将动作结论映射为与目标资源终态分栏展示的中文文案。 */
export function operationResultLabel(result: OperationResult): string {
  const labels: Record<OperationResult, string> = {
    UNKNOWN: "结果未知",
    QUEUED: "已排队",
    RUNNING: "运行中",
    CANCEL_REQUESTED: "取消处理中",
    SUCCEEDED: "动作成功",
    PARTIAL: "部分成功",
    FAILED: "动作失败",
    CANCELLED: "已取消",
    INTERRUPTED: "已中断",
    SKIPPED: "已跳过",
    REJECTED: "已拒绝",
  };

  return labels[result];
}

/** 将健康检查批次状态映射为中文文案。 */
export function healthCheckStatusLabel(status: HealthCheckStatus): string {
  const labels: Record<HealthCheckStatus, string> = {
    QUEUED: "已排队",
    RUNNING: "检查中",
    SUCCEEDED: "检查成功",
    PARTIAL: "部分完成",
    FAILED: "检查失败",
    CANCELLED: "已取消",
    REJECTED: "已拒绝",
  };

  return labels[status];
}

/** 将全局执行槽状态映射为不会误判并发的中文文案。 */
export function executionSlotLabel(state: ExecutionSlotState): string {
  const labels: Record<ExecutionSlotState, string> = {
    IDLE: "空闲",
    RUNNING: "运行中",
    RECOVERING: "恢复中，暂不领取下一任务",
  };

  return labels[state];
}

/** 返回状态对应的 MUI 语义色，但文字始终同时展示。 */
export function statusChipColor(
  status: RunStatus | CommandStatus | HealthStatus | FreshnessStatus | DeliveryStatus,
): ChipProps["color"] {
  if (
    status === "FAILED" ||
    status === "CRITICAL" ||
    status === "STALE" ||
    status === "REJECTED" ||
    status === "DEAD_LETTER"
  ) {
    return "error";
  }
  if (status === "WARN" || status === "WARNING" || status === "PARTIAL") {
    return "warning";
  }
  if (status === "SUCCEEDED" || status === "HEALTHY" || status === "FRESH") {
    return "success";
  }
  if (status === "RUNNING" || status === "DELIVERING" || status === "CANCEL_REQUESTED") {
    return "info";
  }

  return "default";
}

/** 判断 run 是否已进入无需继续轮询的终态。 */
export function isRunTerminal(status: RunStatus): boolean {
  return ["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "SKIPPED"].includes(status);
}

/** 判断 command 是否已进入无需继续轮询的终态。 */
export function isCommandTerminal(status: CommandStatus): boolean {
  return ["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "REJECTED"].includes(status);
}

/** 判断主动健康检查是否已进入无需继续轮询的终态。 */
export function isHealthCheckTerminal(status: HealthCheckStatus): boolean {
  return ["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "REJECTED"].includes(status);
}

/** 判断 submission 是否已结束本地投递阶段。 */
export function isSubmissionDeliveryTerminal(status: DeliveryStatus): boolean {
  return ["ACCEPTED", "REJECTED", "DEAD_LETTER"].includes(status);
}

/** 生成安全失败文本，只包含服务端允许公开的稳定字段。 */
export function errorSummaryLabel(error: ErrorSummary | null): string {
  return error === null ? "—" : `${error.code} · ${error.stage} · ${error.message}`;
}

/** 生成 USER 或 SYSTEM 的公开显示名称，不读取内部主体标识。 */
export function actorDisplayLabel(actor: ActorDisplay): string {
  if (actor.actorType === "SYSTEM") {
    const systemLabels: Record<NonNullable<ActorDisplay["systemKind"]>, string> = {
      SCHEDULE: "系统计划",
      LEGACY: "遗留任务",
      RECOVERY: "系统恢复",
      OTHER: "系统任务",
    };

    return systemLabels[actor.systemKind ?? "OTHER"];
  }

  return actor.deleted ? "已删除用户" : actor.displayName;
}

/** 生成运行进度文本；估算未知时不伪造百分比。 */
export function runProgressLabel(
  processedRecords: number,
  estimatedRecords: number | null,
): string {
  if (estimatedRecords === null || estimatedRecords === 0) {
    return `已处理 ${processedRecords.toLocaleString("zh-CN")} 条`;
  }

  const percentage = Math.min(100, Math.round((processedRecords / estimatedRecords) * 100));
  return `已处理 ${processedRecords.toLocaleString("zh-CN")} / 预计 ${estimatedRecords.toLocaleString(
    "zh-CN",
  )} 条 ${percentage}%`;
}
