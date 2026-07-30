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
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";

import { dataHealthCheckQueryOptions } from "../../../api/data-operations";
import {
  errorSummaryLabel,
  formatDataOperationsDateTime,
  healthCheckStatusLabel,
  isHealthCheckTerminal,
  statusChipColor,
} from "../utils/data-operations-presentation";

/** 描述健康检查批次 Drawer 的导航回调。 */
interface HealthCheckDetailDrawerProps {
  healthCheckId: string | undefined;
  onClose: () => void;
  onOpenEvaluation: (evaluationId: string) => void;
}

/** 展示按原 target 顺序返回的健康检查批次，成功 target 可进入不可变评估。 */
export function HealthCheckDetailDrawer({
  healthCheckId,
  onClose,
  onOpenEvaluation,
}: HealthCheckDetailDrawerProps) {
  const healthCheckQuery = useQuery({
    ...dataHealthCheckQueryOptions(healthCheckId ?? ""),
    enabled: healthCheckId !== undefined,
    /** 健康检查只在非终态轮询，直到全部 target 有权威结果。 */
    refetchInterval: (query) =>
      query.state.data !== undefined && !isHealthCheckTerminal(query.state.data.status)
        ? 2_000
        : false,
  });

  /** 关闭批次 Drawer 并清理 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  return (
    <Drawer
      anchor="right"
      open={healthCheckId !== undefined}
      onClose={handleClose}
      aria-labelledby="health-check-detail-title"
    >
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 2 }}>
        {healthCheckQuery.isPending ? (
          <Skeleton variant="rounded" height={420} aria-label="正在加载健康检查批次" />
        ) : null}
        {healthCheckQuery.isError ? (
          <Alert severity="error">无法读取健康检查批次；不能从评估列表重建 target 顺序。</Alert>
        ) : null}
        {healthCheckQuery.data !== undefined ? (
          <>
            <Typography id="health-check-detail-title" variant="h5">
              主动健康检查批次
            </Typography>
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
              {healthCheckQuery.data.healthCheckId}
            </Typography>
            <Chip
              color={statusChipColor(healthCheckQuery.data.status)}
              label={healthCheckStatusLabel(healthCheckQuery.data.status)}
            />
            <Typography variant="caption" color="text.secondary">
              请求 {formatDataOperationsDateTime(healthCheckQuery.data.requestedAt)}
            </Typography>
            <Divider />
            <List disablePadding aria-label="健康检查目标结果">
              {/* targets 是服务端保留的原提交顺序，批次 partial 不覆盖已成功 target。 */}
              {healthCheckQuery.data.targets.map((target, index) => (
                <ListItem
                  key={`${target.target.datasetCode}-${index}`}
                  disableGutters
                  secondaryAction={
                    target.evaluationId !== null ? (
                      <Button
                        size="small"
                        onClick={() => onOpenEvaluation(target.evaluationId ?? "")}
                      >
                        查看评估
                      </Button>
                    ) : undefined
                  }
                >
                  <ListItemText
                    primary={`${index + 1}. ${target.target.datasetCode} · ${target.status}`}
                    secondary={`绑定版本 ${target.resolvedDataVersion ?? "尚未绑定"} · ${errorSummaryLabel(target.error)}`}
                  />
                </ListItem>
              ))}
            </List>
          </>
        ) : null}
      </Box>
    </Drawer>
  );
}
