import {
  Alert,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";
import type { ChangeEvent } from "react";

import {
  createDataOperationIdempotencyKey,
  submitDatasetHealthCheck,
} from "../../../api/data-operations";
import { isApiError } from "../../../api/http";
import type { DatasetSummary, SubmissionReceipt } from "../../../types/data-operations";

/** 描述主动健康检查 Dialog 的数据集和 submission 回调。 */
interface HealthCheckDialogProps {
  open: boolean;
  datasets: DatasetSummary[];
  initialDatasets: DatasetSummary[];
  onClose: () => void;
  onSubmission: (receipt: SubmissionReceipt) => void;
}

/** 为每个 target 独立选择数据版本并提交主动健康检查意图。 */
export function HealthCheckDialog({
  open,
  datasets,
  initialDatasets,
  onClose,
  onSubmission,
}: HealthCheckDialogProps) {
  const queryClient = useQueryClient();
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(
    () => new Set(initialDatasets.map((dataset) => dataset.datasetCode)),
  );
  const [dataVersions, setDataVersions] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const idempotencyKeyRef = useRef<string | undefined>(undefined);
  const selectedDatasets = useMemo(
    () => datasets.filter((dataset) => selectedCodes.has(dataset.datasetCode)),
    [datasets, selectedCodes],
  );
  const mutation = useMutation({
    mutationFn: async (): Promise<SubmissionReceipt> => {
      const idempotencyKey = idempotencyKeyRef.current ?? createDataOperationIdempotencyKey();
      idempotencyKeyRef.current = idempotencyKey;
      return submitDatasetHealthCheck(
        {
          // 每个 target 独立绑定 dataVersion；空值表示受理时绑定最新 production publication。
          targets: selectedDatasets.map((dataset) => ({
            datasetCode: dataset.datasetCode,
            dataVersion: dataVersions[dataset.datasetCode] || null,
          })),
          reason,
        },
        { idempotencyKey },
      );
    },
    /** 提交持久化后刷新公开投影，并由 submission 跟踪批次权威状态。 */
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ["dataOperations"] });
      onSubmission(receipt);
    },
  });

  /** 切换一个数据集 target，集合天然保证同批 datasetCode 唯一。 */
  const handleToggleDataset = useCallback((datasetCode: string) => {
    setSelectedCodes((current) => {
      const next = new Set(current);
      if (next.has(datasetCode)) next.delete(datasetCode);
      else next.add(datasetCode);
      return next;
    });
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 更新当前 target 的可选历史数据版本。 */
  const handleDataVersionChange = useCallback((datasetCode: string, value: string) => {
    setDataVersions((current) => ({ ...current, [datasetCode]: value }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 更新公开审计原因。 */
  const handleReasonChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setReason(event.target.value);
  }, []);

  /** 提交完整的批量健康检查意图。 */
  const handleSubmit = useCallback(() => {
    if (selectedDatasets.length > 0 && reason.trim().length >= 2) mutation.mutate();
  }, [mutation, reason, selectedDatasets.length]);

  /** 关闭焦点任务，不把版本草稿或原因留入 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  const errorCode = isApiError(mutation.error) ? mutation.error.code : undefined;

  return (
    <Dialog open={open} onClose={handleClose} aria-labelledby="health-check-dialog-title">
      <DialogTitle id="health-check-dialog-title">主动健康检查</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Alert severity="info">
            每个数据集独立绑定版本；留空时在受理时绑定该数据集最新 production publication。
          </Alert>
          {/* 逐数据集渲染 target，禁止用一个共享 dataVersion 覆盖批量检查。 */}
          {datasets.map((dataset) => {
            const selected = selectedCodes.has(dataset.datasetCode);
            return (
              <Stack key={dataset.datasetCode} direction="row" spacing={1} alignItems="center">
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={selected}
                      onChange={() => handleToggleDataset(dataset.datasetCode)}
                    />
                  }
                  label={<Typography variant="body2">{dataset.displayName}</Typography>}
                  sx={{ width: 280 }}
                />
                <TextField
                  label="指定 dataVersion（可选）"
                  value={dataVersions[dataset.datasetCode] ?? ""}
                  onChange={(event) =>
                    handleDataVersionChange(dataset.datasetCode, event.target.value)
                  }
                  disabled={!selected}
                  size="small"
                  sx={{ flex: 1 }}
                />
              </Stack>
            );
          })}
          <TextField
            label="操作原因"
            value={reason}
            onChange={handleReasonChange}
            required
            multiline
            minRows={2}
            error={reason.length > 0 && reason.trim().length < 2}
            helperText="至少 2 个字符；原因将进入操作记录。"
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
          disabled={selectedDatasets.length === 0 || reason.trim().length < 2 || mutation.isPending}
        >
          {mutation.isPending ? "正在提交" : `提交检查意图（${selectedDatasets.length}）`}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
