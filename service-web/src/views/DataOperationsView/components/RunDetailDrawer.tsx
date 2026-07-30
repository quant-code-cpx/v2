import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  Drawer,
  LinearProgress,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { dataSyncRunDetailQueryOptions } from "../../../api/data-operations";
import type { CommandActionTarget } from "../../../types/data-operations";
import {
  actorDisplayLabel,
  errorSummaryLabel,
  formatDataOperationsDateTime,
  isRunTerminal,
  runProgressLabel,
  runStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";

/** 描述运行详情 Drawer 的独立 cursor 和显式操作入口。 */
interface RunDetailDrawerProps {
  runId: string | undefined;
  canWrite: boolean;
  onClose: () => void;
  onAction: (action: "cancel" | "retry", target: CommandActionTarget) => void;
}

/** 展示 run 受理快照、质量门、独立分区 cursor 与时间线 cursor。 */
export function RunDetailDrawer({ runId, canWrite, onClose, onAction }: RunDetailDrawerProps) {
  const [partitionsCursor, setPartitionsCursor] = useState<string | null>(null);
  const [timelineCursor, setTimelineCursor] = useState<string | null>(null);
  const runQuery = useQuery({
    ...dataSyncRunDetailQueryOptions({
      runId: runId ?? "",
      partitionsCursor,
      partitionsLimit: 100,
      timelineCursor,
      timelineLimit: 100,
    }),
    enabled: runId !== undefined,
    /** 运行只在非终态轮询，`INTERRUPTED` 仍等待服务端恢复结论。 */
    refetchInterval: (query) =>
      query.state.data !== undefined && !isRunTerminal(query.state.data.run.status) ? 2_000 : false,
  });

  /** 关闭详情并由父级清理 URL 标识。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 仅推进分区 cursor，时间线 cursor 保持独立。 */
  const handleLoadMorePartitions = useCallback(() => {
    if (
      runQuery.data?.partitionsNextCursor !== null &&
      runQuery.data?.partitionsNextCursor !== undefined
    ) {
      setPartitionsCursor(runQuery.data.partitionsNextCursor);
    }
  }, [runQuery.data?.partitionsNextCursor]);

  /** 仅推进时间线 cursor，分区 cursor 保持独立。 */
  const handleLoadMoreTimeline = useCallback(() => {
    if (
      runQuery.data?.timelineNextCursor !== null &&
      runQuery.data?.timelineNextCursor !== undefined
    ) {
      setTimelineCursor(runQuery.data.timelineNextCursor);
    }
  }, [runQuery.data?.timelineNextCursor]);

  /** 对当前单 run 请求合作式取消。 */
  const handleCancel = useCallback(() => {
    if (runId !== undefined) onAction("cancel", { resourceType: "RUN", resourceId: runId });
  }, [onAction, runId]);

  /** 对当前单 run 请求服务端判定的重试。 */
  const handleRetry = useCallback(() => {
    if (runId !== undefined) onAction("retry", { resourceType: "RUN", resourceId: runId });
  }, [onAction, runId]);

  return (
    <Drawer
      anchor="right"
      open={runId !== undefined}
      onClose={handleClose}
      aria-labelledby="run-detail-title"
    >
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 2 }}>
        {runQuery.isPending ? (
          <Skeleton variant="rounded" height={520} aria-label="正在加载运行详情" />
        ) : null}
        {runQuery.isError ? (
          <Alert severity="error">
            无法读取运行详情；安全响应不包含 checkpoint 或 fencing token。
          </Alert>
        ) : null}
        {runQuery.data !== undefined ? (
          <>
            <Box>
              <Typography id="run-detail-title" variant="h5">
                运行详情
              </Typography>
              <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
                {runQuery.data.run.runId}
              </Typography>
            </Box>
            <Stack direction="row" spacing={1} alignItems="center">
              <Chip
                color={statusChipColor(runQuery.data.run.status)}
                label={runStatusLabel(runQuery.data.run.status)}
              />
              <Typography variant="caption" color="text.secondary">
                请求 {formatDataOperationsDateTime(runQuery.data.run.requestedAt)}
              </Typography>
            </Stack>
            <Typography variant="body2">
              {runProgressLabel(
                runQuery.data.run.progress.processedRecords,
                runQuery.data.run.progress.estimatedRecords,
              )}
            </Typography>
            {runQuery.data.run.progress.estimatedRecords !== null &&
            runQuery.data.run.progress.estimatedRecords > 0 ? (
              <LinearProgress
                variant="determinate"
                value={Math.min(
                  100,
                  (runQuery.data.run.progress.processedRecords /
                    runQuery.data.run.progress.estimatedRecords) *
                    100,
                )}
              />
            ) : (
              <LinearProgress />
            )}
            <Divider />
            <Box component="section" aria-labelledby="run-source-snapshot-title">
              <Typography id="run-source-snapshot-title" variant="subtitle1">
                受理时来源快照
              </Typography>
              {/* 历史运行必须使用冻结 `sourceSnapshot`，不能以当前目录来源回填。 */}
              {runQuery.data.sourceSnapshot.map((source) => (
                <Typography
                  key={`${source.providerId}-${source.upstreamSource}-${source.sourceDataset}`}
                  variant="body2"
                  sx={{ mt: 0.75 }}
                >
                  {source.providerId} / {source.upstreamSource} / {source.sourceDataset} ·{" "}
                  {source.adapterId} · {source.methodologyCode} v{source.methodologyVersion}
                </Typography>
              ))}
            </Box>
            <Divider />
            <Box component="section" aria-labelledby="run-quality-gate-title">
              <Typography id="run-quality-gate-title" variant="subtitle1">
                发布前质量门
              </Typography>
              <Typography variant="body2">
                {runQuery.data.qualityGate.disposition} · 受影响{" "}
                {runQuery.data.qualityGate.affectedCount ?? "—"} 项
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {runQuery.data.qualityGate.disposition === "BLOCKED"
                  ? "质量门阻止新 publication；既有 publication 保持可见。"
                  : "发布前质量门与发布后健康评估是两条独立事实。"}{" "}
                {errorSummaryLabel(runQuery.data.qualityGate.error)}
              </Typography>
            </Box>
            <Divider />
            <Box component="section" aria-labelledby="run-partitions-title">
              <Typography id="run-partitions-title" variant="subtitle1">
                分区（{runQuery.data.partitionCount}）
              </Typography>
              {/* 仅显示公开分区状态与脱敏错误，不消费内部 checkpoint。 */}
              {runQuery.data.partitions.map((partition) => (
                <Typography key={partition.partitionKey} variant="body2" sx={{ mt: 0.75 }}>
                  {partition.partitionKey} · {runStatusLabel(partition.status)} · 尝试{" "}
                  {partition.attempt} · {errorSummaryLabel(partition.error)}
                </Typography>
              ))}
              {runQuery.data.partitionsNextCursor !== null ? (
                <Button size="small" onClick={handleLoadMorePartitions}>
                  加载更多分区
                </Button>
              ) : null}
            </Box>
            <Divider />
            <Box component="section" aria-labelledby="run-timeline-title">
              <Typography id="run-timeline-title" variant="subtitle1">
                运行时间线（{runQuery.data.timelineEventCount}）
              </Typography>
              {/* 时间线只使用公开 ActorDisplay 和稳定 requestId。 */}
              {runQuery.data.timeline.map((event) => (
                <Typography key={event.eventId} variant="body2" sx={{ mt: 0.75 }}>
                  {formatDataOperationsDateTime(event.occurredAt)} ·{" "}
                  {actorDisplayLabel(event.actor)} · {event.action} / {event.result} ·{" "}
                  {event.requestId}
                </Typography>
              ))}
              {runQuery.data.timelineNextCursor !== null ? (
                <Button size="small" onClick={handleLoadMoreTimeline}>
                  加载更多事件
                </Button>
              ) : null}
            </Box>
            {canWrite ? (
              <Stack direction="row" spacing={1} sx={{ mt: "auto" }}>
                {runQuery.data.run.status === "QUEUED" || runQuery.data.run.status === "RUNNING" ? (
                  <Button color="warning" onClick={handleCancel}>
                    取消此 RUN
                  </Button>
                ) : null}
                {(runQuery.data.run.status === "PARTIAL" ||
                  runQuery.data.run.status === "FAILED" ||
                  runQuery.data.run.status === "INTERRUPTED") &&
                runQuery.data.run.error?.retryable === true ? (
                  <Button onClick={handleRetry}>重试此 RUN</Button>
                ) : null}
              </Stack>
            ) : null}
          </>
        ) : null}
      </Box>
    </Drawer>
  );
}
