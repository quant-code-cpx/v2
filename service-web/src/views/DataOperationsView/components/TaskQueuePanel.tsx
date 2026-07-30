import {
  Alert,
  Box,
  Button,
  Card,
  Chip,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";

import type { CommandActionTarget, RunPage } from "../../../types/data-operations";
import {
  formatDataOperationsDateTime,
  runProgressLabel,
  runStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";
import { CursorPager } from "./CursorPager";
import { DataOperationsTableEmptyState } from "./DataOperationsTableEmptyState";

/** 描述同步队列面板的远程列表与显式动作入口。 */
interface TaskQueuePanelProps {
  data: RunPage | undefined;
  isLoading: boolean;
  isError: boolean;
  canWrite: boolean;
  cursor?: string;
  onPageChange: (cursor: string | undefined) => void;
  onRefresh: () => void;
  onOpenRun: (runId: string) => void;
  onOpenCommand: (commandId: string) => void;
  onAction: (action: "cancel" | "retry", target: CommandActionTarget) => void;
}

/** 展示全局串行队列与历史运行，批次详情始终跳转 commands/detail。 */
export function TaskQueuePanel({
  data,
  isLoading,
  isError,
  canWrite,
  cursor,
  onPageChange,
  onRefresh,
  onOpenRun,
  onOpenCommand,
  onAction,
}: TaskQueuePanelProps) {
  return (
    <Stack spacing={2} component="section" aria-labelledby="task-queue-title">
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end">
        <Box>
          <Typography id="task-queue-title" variant="h4">
            同步任务
          </Typography>
        </Box>
      </Stack>
      {isError && data === undefined ? (
        <Alert
          severity="error"
          action={
            <Button color="inherit" onClick={onRefresh}>
              重试
            </Button>
          }
        >
          无法读取同步队列；不会显示为没有任务。
        </Alert>
      ) : null}
      <Card>
        <TableContainer sx={{ overflowX: "auto" }}>
          <Table size="small" aria-label="同步任务列表">
            <TableHead>
              <TableRow>
                <TableCell>数据集 / 模式</TableCell>
                <TableCell>状态 / 队列</TableCell>
                <TableCell>进度</TableCell>
                <TableCell>请求 / 开始</TableCell>
                <TableCell align="right">操作</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {/* 保持服务端 run 列表顺序；不尝试按 commandId 在客户端重建批次。 */}
              {data !== undefined && data.items.length === 0 ? (
                <DataOperationsTableEmptyState
                  colSpan={5}
                  title="暂无同步任务"
                  description="新建同步任务后会在这里显示执行进度和结果。"
                />
              ) : null}
              {data?.items.map((run) => {
                const canCancel = run.status === "QUEUED" || run.status === "RUNNING";
                const canRetry =
                  (run.status === "PARTIAL" ||
                    run.status === "FAILED" ||
                    run.status === "INTERRUPTED") &&
                  run.error?.retryable === true;
                return (
                  <TableRow key={run.runId} hover>
                    <TableCell>
                      <Typography fontWeight={700}>{run.datasetCode}</Typography>
                      <Typography variant="caption" color="text.secondary">
                        {run.mode}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={1} alignItems="center">
                        <Chip
                          size="small"
                          color={statusChipColor(run.status)}
                          label={runStatusLabel(run.status)}
                        />
                        <Typography variant="caption" color="text.secondary">
                          {run.queuePosition === null ? "—" : `队列 ${run.queuePosition}`}
                        </Typography>
                      </Stack>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {runProgressLabel(
                          run.progress.processedRecords,
                          run.progress.estimatedRecords,
                        )}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        分区 {run.progress.completedPartitions} / {run.progress.totalPartitions}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        请求 {formatDataOperationsDateTime(run.requestedAt)}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        开始 {formatDataOperationsDateTime(run.startedAt)}
                      </Typography>
                    </TableCell>
                    <TableCell align="right">
                      <Stack direction="row" spacing={0.5} justifyContent="flex-end">
                        <Button size="small" onClick={() => onOpenRun(run.runId)}>
                          运行详情
                        </Button>
                        <Button size="small" onClick={() => onOpenCommand(run.commandId)}>
                          命令详情
                        </Button>
                        {canWrite && canCancel ? (
                          <Button
                            size="small"
                            color="warning"
                            onClick={() =>
                              onAction("cancel", { resourceType: "RUN", resourceId: run.runId })
                            }
                          >
                            取消此 RUN
                          </Button>
                        ) : null}
                        {canWrite && canRetry ? (
                          <Button
                            size="small"
                            onClick={() =>
                              onAction("retry", { resourceType: "RUN", resourceId: run.runId })
                            }
                          >
                            重试此 RUN
                          </Button>
                        ) : null}
                      </Stack>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
        {data !== undefined ? (
          <CursorPager
            currentCursor={cursor}
            nextCursor={data.nextCursor}
            isLoading={isLoading}
            onPageChange={onPageChange}
          />
        ) : null}
      </Card>
    </Stack>
  );
}
