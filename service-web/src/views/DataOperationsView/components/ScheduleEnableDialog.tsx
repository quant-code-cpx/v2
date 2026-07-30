import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import type { ChangeEvent } from "react";

import {
  createDataOperationIdempotencyKey,
  setDataSyncScheduleEnabled,
} from "../../../api/data-operations";
import { isApiError } from "../../../api/http";
import type { ScheduleView, SubmissionReceipt } from "../../../types/data-operations";

/** 描述计划启停 Dialog 所需的计划与 submission 回调。 */
interface ScheduleEnableDialogProps {
  schedule: ScheduleView | undefined;
  onClose: () => void;
  onSubmission: (receipt: SubmissionReceipt) => void;
}

/** 以乐观版本和必填原因提交计划启停意图。 */
export function ScheduleEnableDialog({
  schedule,
  onClose,
  onSubmission,
}: ScheduleEnableDialogProps) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const idempotencyKeyRef = useRef<string | undefined>(undefined);
  const mutation = useMutation({
    mutationFn: async (): Promise<SubmissionReceipt> => {
      if (schedule === undefined) throw new Error("未指定计划。");
      const idempotencyKey = idempotencyKeyRef.current ?? createDataOperationIdempotencyKey();
      idempotencyKeyRef.current = idempotencyKey;
      return setDataSyncScheduleEnabled(
        {
          scheduleId: schedule.scheduleId,
          enabled: !schedule.enabled,
          expectedVersion: schedule.version,
          reason,
        },
        { idempotencyKey },
      );
    },
    /** 意图持久化后刷新计划投影，并交由 submission 对账。 */
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ["dataOperations"] });
      onSubmission(receipt);
    },
  });

  /** 更新计划启停审计原因。 */
  const handleReasonChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setReason(event.target.value);
  }, []);

  /** 提交带版本的启停意图。 */
  const handleSubmit = useCallback(() => {
    if (schedule !== undefined && reason.trim().length >= 2) mutation.mutate();
  }, [mutation, reason, schedule]);

  /** 关闭计划启停短任务。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  const errorCode = isApiError(mutation.error) ? mutation.error.code : undefined;
  return (
    <Dialog
      open={schedule !== undefined}
      onClose={handleClose}
      aria-labelledby="schedule-enable-dialog-title"
    >
      <DialogTitle id="schedule-enable-dialog-title">
        {schedule?.enabled ? "暂停自动计划" : "启用自动计划"}
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography variant="body2">
            {schedule?.datasetCode} · 当前版本 {schedule?.version}
          </Typography>
          <Alert severity="warning">计划触发仍会进入同一个全局串行队列，不会并行执行同步。</Alert>
          <TextField
            label="操作原因"
            value={reason}
            onChange={handleReasonChange}
            required
            multiline
            minRows={2}
            error={reason.length > 0 && reason.trim().length < 2}
            helperText="至少 2 个字符；将进入公开操作记录。"
          />
          {errorCode !== undefined ? <Alert severity="error">提交失败：{errorCode}</Alert> : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={mutation.isPending}>
          取消
        </Button>
        <Button
          variant="contained"
          onClick={handleSubmit}
          disabled={reason.trim().length < 2 || mutation.isPending}
        >
          {mutation.isPending ? "正在提交" : "提交计划意图"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
