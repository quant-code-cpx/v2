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
  cancelDataSync,
  createDataOperationIdempotencyKey,
  retryDataSync,
} from "../../../api/data-operations";
import { isApiError } from "../../../api/http";
import type { CommandActionTarget, SubmissionReceipt } from "../../../types/data-operations";

/** 描述取消或重试 Dialog 所需的显式目标和完成回调。 */
interface CommandActionDialogProps {
  action: "cancel" | "retry" | undefined;
  target: CommandActionTarget | undefined;
  onClose: () => void;
  onSubmission: (receipt: SubmissionReceipt) => void;
}

/** 以明确 COMMAND 或 RUN 作用域提交取消、重试与强制审计原因。 */
export function CommandActionDialog({
  action,
  target,
  onClose,
  onSubmission,
}: CommandActionDialogProps) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const idempotencyKeyRef = useRef<string | undefined>(undefined);
  const mutation = useMutation({
    mutationFn: async (): Promise<SubmissionReceipt> => {
      if (action === undefined || target === undefined) {
        throw new Error("未指定同步操作目标。");
      }
      const idempotencyKey = idempotencyKeyRef.current ?? createDataOperationIdempotencyKey();
      idempotencyKeyRef.current = idempotencyKey;
      const input = { target, reason };
      return action === "cancel"
        ? cancelDataSync(input, { idempotencyKey })
        : retryDataSync(input, { idempotencyKey });
    },
    /** 持久化意图后刷新状态投影，再由 submission 跟踪权威结论。 */
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ["dataOperations"] });
      onSubmission(receipt);
    },
  });

  /** 关闭短任务 Dialog，不将操作原因保存在 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 更新动作审计原因。 */
  const handleReasonChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setReason(event.target.value);
  }, []);

  /** 仅在目标、原因和动作都完整时提交稳定幂等意图。 */
  const handleSubmit = useCallback(() => {
    if (target !== undefined && action !== undefined && reason.trim().length >= 2) {
      mutation.mutate();
    }
  }, [action, mutation, reason, target]);

  const errorCode = isApiError(mutation.error) ? mutation.error.code : undefined;
  const title = action === "cancel" ? "请求取消同步" : "重试同步";

  return (
    <Dialog
      open={action !== undefined && target !== undefined}
      onClose={handleClose}
      aria-labelledby="command-action-dialog-title"
    >
      <DialogTitle id="command-action-dialog-title">{title}</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Alert severity={action === "cancel" ? "warning" : "info"}>
            作用域：{target?.resourceType} · {target?.resourceId}
          </Alert>
          <Typography variant="body2" color="text.secondary">
            {action === "cancel"
              ? "COMMAND 会影响整批 child run，RUN 只作用当前子运行。运行中任务为合作式取消，取消处理中不表示已取消。"
              : "重试只对服务端判定可重试的失败、部分成功或中断目标生效，并创建新的可追踪命令。"}
          </Typography>
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
          {mutation.isPending ? "正在提交" : "提交操作意图"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
