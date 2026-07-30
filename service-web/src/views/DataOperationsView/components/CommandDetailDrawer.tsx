import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  Drawer,
  List,
  ListItem,
  ListItemText,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";

import { dataSyncCommandQueryOptions } from "../../../api/data-operations";
import type { CommandActionTarget } from "../../../types/data-operations";
import {
  actorDisplayLabel,
  commandStatusLabel,
  formatDataOperationsDateTime,
  isCommandTerminal,
  runStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";

/** 描述命令详情 Drawer 的导航与受控操作入口。 */
interface CommandDetailDrawerProps {
  commandId: string | undefined;
  canWrite: boolean;
  onClose: () => void;
  onOpenRun: (runId: string) => void;
  onAction: (action: "cancel" | "retry", target: CommandActionTarget) => void;
}

/** 展示命令权威聚合状态和服务端保留的 child run 提交顺序。 */
export function CommandDetailDrawer({
  commandId,
  canWrite,
  onClose,
  onOpenRun,
  onAction,
}: CommandDetailDrawerProps) {
  const commandQuery = useQuery({
    ...dataSyncCommandQueryOptions(commandId ?? ""),
    enabled: commandId !== undefined,
    /** 命令只在非终态时轮询，终态立即停止。 */
    refetchInterval: (query) =>
      query.state.data !== undefined && !isCommandTerminal(query.state.data.status) ? 2_000 : false,
  });

  /** 关闭命令详情并由父级清理 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 对整批命令请求合作式取消。 */
  const handleCancelCommand = useCallback(() => {
    if (commandId !== undefined)
      onAction("cancel", { resourceType: "COMMAND", resourceId: commandId });
  }, [commandId, onAction]);

  /** 对服务端可重试的整批命令请求重试。 */
  const handleRetryCommand = useCallback(() => {
    if (commandId !== undefined)
      onAction("retry", { resourceType: "COMMAND", resourceId: commandId });
  }, [commandId, onAction]);

  return (
    <Drawer
      anchor="right"
      open={commandId !== undefined}
      onClose={handleClose}
      aria-labelledby="command-detail-title"
    >
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 2 }}>
        {commandQuery.isPending ? (
          <Skeleton variant="rounded" height={420} aria-label="正在加载命令详情" />
        ) : null}
        {commandQuery.isError ? (
          <Alert severity="error">无法读取命令详情；不能从运行列表重建批次。</Alert>
        ) : null}
        {commandQuery.data !== undefined ? (
          <>
            <Typography id="command-detail-title" variant="h5">
              批量命令
            </Typography>
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
              {commandQuery.data.commandId}
            </Typography>
            <Stack direction="row" spacing={1} alignItems="center">
              <Chip
                color={statusChipColor(commandQuery.data.status)}
                label={commandStatusLabel(commandQuery.data.status)}
              />
              <Typography variant="caption" color="text.secondary">
                请求 {formatDataOperationsDateTime(commandQuery.data.requestedAt)}
              </Typography>
            </Stack>
            <Typography variant="body2" color="text.secondary">
              提交人：{actorDisplayLabel(commandQuery.data.requestedBy)} · child runs 按原 target
              提交顺序展示。
            </Typography>
            <Divider />
            <List disablePadding aria-label="命令子运行">
              {/* childRuns 是服务端唯一聚合与排序来源。 */}
              {commandQuery.data.childRuns.map((run, index) => (
                <ListItem
                  key={run.runId}
                  secondaryAction={
                    <Button size="small" onClick={() => onOpenRun(run.runId)}>
                      查看 RUN
                    </Button>
                  }
                  disableGutters
                >
                  <ListItemText
                    primary={`${index + 1}. ${run.datasetCode} · ${runStatusLabel(run.status)}`}
                    secondary={`${run.mode} · 队列 ${run.queuePosition ?? "—"}`}
                  />
                </ListItem>
              ))}
            </List>
            {canWrite ? (
              <Stack direction="row" spacing={1} sx={{ mt: "auto" }}>
                {commandQuery.data.status === "QUEUED" || commandQuery.data.status === "RUNNING" ? (
                  <Button color="warning" onClick={handleCancelCommand}>
                    取消整批 COMMAND
                  </Button>
                ) : null}
                {(commandQuery.data.status === "PARTIAL" ||
                  commandQuery.data.status === "FAILED") &&
                commandQuery.data.error?.retryable === true ? (
                  <Button onClick={handleRetryCommand}>重试整批 COMMAND</Button>
                ) : null}
              </Stack>
            ) : null}
          </>
        ) : null}
      </Box>
    </Drawer>
  );
}
