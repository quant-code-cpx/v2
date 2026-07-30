import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  Drawer,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";

import { dataOperationSubmissionQueryOptions } from "../../../api/data-operations";
import { isApiError } from "../../../api/http";
import type { DataOperationResourceType } from "../../../types/data-operations";
import {
  deliveryStatusLabel,
  errorSummaryLabel,
  isSubmissionDeliveryTerminal,
  operationResultLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";

/** 描述 submission 对账 Drawer 的权威资源导航接口。 */
interface SubmissionTrackerDrawerProps {
  submissionId: string | undefined;
  onClose: () => void;
  onOpenResource: (resourceType: DataOperationResourceType, resourceId: string) => void;
}

/** 持续对账 PENDING 意图到权威资源或异步拒绝，不把首次 202 误称下游成功。 */
export function SubmissionTrackerDrawer({
  submissionId,
  onClose,
  onOpenResource,
}: SubmissionTrackerDrawerProps) {
  const submissionQuery = useQuery({
    ...dataOperationSubmissionQueryOptions(submissionId ?? ""),
    enabled: submissionId !== undefined,
    /** 仅在 PENDING/DELIVERING 阶段轮询；接受、拒绝或死信立即停止。 */
    refetchInterval: (query) =>
      query.state.data !== undefined &&
      !isSubmissionDeliveryTerminal(query.state.data.deliveryStatus)
        ? 2_000
        : false,
  });

  /** 关闭 submission 对账视图并清理 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 跳转到 API 已接受的 COMMAND、RUN、HEALTH_CHECK 或 SCHEDULE 权威资源。 */
  const handleOpenResource = useCallback(() => {
    if (
      submissionQuery.data?.authorityResource !== null &&
      submissionQuery.data?.authorityResource !== undefined
    ) {
      onOpenResource(
        submissionQuery.data.authorityResource.resourceType,
        submissionQuery.data.authorityResource.resourceId,
      );
    }
  }, [onOpenResource, submissionQuery.data?.authorityResource]);

  const errorCode = isApiError(submissionQuery.error) ? submissionQuery.error.code : undefined;
  return (
    <Drawer
      anchor="right"
      open={submissionId !== undefined}
      onClose={handleClose}
      aria-labelledby="submission-tracker-title"
    >
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 2 }}>
        {submissionQuery.isPending ? (
          <Skeleton variant="rounded" height={240} aria-label="正在对账提交意图" />
        ) : null}
        {submissionQuery.isError ? (
          <Alert severity="error">无法对账提交意图：{errorCode ?? "submission-unavailable"}</Alert>
        ) : null}
        {submissionQuery.data !== undefined ? (
          <>
            <Typography id="submission-tracker-title" variant="h5">
              提交意图
            </Typography>
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
              {submissionQuery.data.submissionId}
            </Typography>
            <Chip
              color={statusChipColor(submissionQuery.data.deliveryStatus)}
              label={deliveryStatusLabel(submissionQuery.data.deliveryStatus)}
            />
            <Typography variant="body2">动作：{submissionQuery.data.action}</Typography>
            <Typography variant="body2">
              动作结果：{operationResultLabel(submissionQuery.data.operationResult)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              requestId：{submissionQuery.data.requestId}
            </Typography>
            <Divider />
            {submissionQuery.data.deliveryStatus === "PENDING" ? (
              <Alert severity="info">已提交，等待同步服务受理。</Alert>
            ) : null}
            {submissionQuery.data.deliveryStatus === "REJECTED" ? (
              <Alert severity="error">
                同步服务异步拒绝：{errorSummaryLabel(submissionQuery.data.error)}
              </Alert>
            ) : null}
            {submissionQuery.data.deliveryStatus === "DEAD_LETTER" ? (
              <Alert severity="error">
                投递进入死信：{errorSummaryLabel(submissionQuery.data.error)}
              </Alert>
            ) : null}
            {submissionQuery.data.authorityResource !== null ? (
              <Stack spacing={1}>
                <Typography variant="body2">
                  权威资源：{submissionQuery.data.authorityResource.resourceType} ·{" "}
                  {submissionQuery.data.authorityResource.resourceId}
                </Typography>
                {submissionQuery.data.queuePosition !== null ? (
                  <Typography variant="caption" color="text.secondary">
                    首个 child run 受理快照位置：{submissionQuery.data.queuePosition}
                  </Typography>
                ) : null}
                <Button onClick={handleOpenResource}>查看权威资源</Button>
              </Stack>
            ) : null}
          </>
        ) : null}
      </Box>
    </Drawer>
  );
}
