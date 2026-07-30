import { ErrorOutline as ErrorOutlineIcon } from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
  type SelectChangeEvent,
} from "@mui/material";
import { useCallback, useMemo } from "react";
import type { ChangeEvent } from "react";

import type {
  DatasetSummary,
  SubmissionReceipt,
  SyncMode,
  SyncPreflightTarget,
  TargetSelector,
} from "../../../types/data-operations";
import { useSyncCommandDialog } from "../hooks/useSyncCommandDialog";
import { targetSelectorSummary } from "../utils/target-selector";
import { TargetSelectorEditor } from "./TargetSelectorEditor";

/** 描述同步 Dialog 的可见性、当前目标与 submission 回调。 */
interface SyncCommandDialogProps {
  open: boolean;
  datasets: DatasetSummary[];
  onClose: () => void;
  onSubmission: (receipt: SubmissionReceipt) => void;
}

/** 按一个数据集 capability 渲染模式与日期形状，禁止使用硬编码能力集合。 */
function SyncTargetEditor({
  dataset,
  target,
  onModeChange,
  onDateFromChange,
  onDateToChange,
  onObservationDateChange,
  onSelectorChange,
}: {
  dataset: DatasetSummary;
  target: SyncPreflightTarget | undefined;
  onModeChange: (datasetCode: string, mode: SyncMode) => void;
  onDateFromChange: (datasetCode: string, value: string | null) => void;
  onDateToChange: (datasetCode: string, value: string | null) => void;
  onObservationDateChange: (datasetCode: string, value: string | null) => void;
  onSelectorChange: (datasetCode: string, selector: TargetSelector) => void;
}) {
  /** 仅接受服务端为当前数据集声明的模式。 */
  const handleModeChange = useCallback(
    (event: SelectChangeEvent) => {
      onModeChange(dataset.datasetCode, event.target.value as SyncMode);
    },
    [dataset.datasetCode, onModeChange],
  );

  /** 更新日期范围起点。 */
  const handleDateFromChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onDateFromChange(
        dataset.datasetCode,
        event.target.value.length === 0 ? null : event.target.value,
      );
    },
    [dataset.datasetCode, onDateFromChange],
  );

  /** 更新日期范围终点。 */
  const handleDateToChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onDateToChange(
        dataset.datasetCode,
        event.target.value.length === 0 ? null : event.target.value,
      );
    },
    [dataset.datasetCode, onDateToChange],
  );

  /** 更新观察日。 */
  const handleObservationDateChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onObservationDateChange(
        dataset.datasetCode,
        event.target.value.length === 0 ? null : event.target.value,
      );
    },
    [dataset.datasetCode, onObservationDateChange],
  );

  /** 将当前数据集的受限 selector 变更回传给同步表单状态。 */
  const handleSelectorChange = useCallback(
    (selector: TargetSelector) => {
      onSelectorChange(dataset.datasetCode, selector);
    },
    [dataset.datasetCode, onSelectorChange],
  );

  if (target === undefined) {
    return <Alert severity="warning">该数据集当前不支持人工同步，不能加入提交目标。</Alert>;
  }

  return (
    <Card variant="outlined">
      <CardContent>
        <Stack spacing={1.5}>
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Box sx={{ width: 230, flexShrink: 0 }}>
              <Typography fontWeight={700}>{dataset.displayName}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ fontFamily: "monospace" }}>
                {dataset.datasetCode}
              </Typography>
            </Box>
            <FormControl sx={{ width: 180 }}>
              <InputLabel id={`sync-mode-${dataset.datasetCode}`}>同步模式</InputLabel>
              <Select
                labelId={`sync-mode-${dataset.datasetCode}`}
                label="同步模式"
                value={target.mode}
                onChange={handleModeChange}
              >
                {/* 仅渲染此数据集 `supportedModes`，不以代码或日期猜测能力。 */}
                {dataset.capability.supportedModes.map((mode) => (
                  <MenuItem key={mode} value={mode}>
                    {mode}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            {target.mode === "DATE_RANGE" ? (
              <Stack direction="row" spacing={1}>
                <TextField
                  label="开始日期"
                  type="date"
                  value={target.dateFrom ?? ""}
                  onChange={handleDateFromChange}
                  slotProps={{ inputLabel: { shrink: true } }}
                />
                <TextField
                  label="结束日期"
                  type="date"
                  value={target.dateTo ?? ""}
                  onChange={handleDateToChange}
                  slotProps={{ inputLabel: { shrink: true } }}
                />
              </Stack>
            ) : null}
            {target.mode === "OBSERVATION_DATE" ? (
              <TextField
                label="观察日"
                type="date"
                value={target.observationDate ?? ""}
                onChange={handleObservationDateChange}
                slotProps={{ inputLabel: { shrink: true } }}
              />
            ) : null}
          </Stack>
          <TargetSelectorEditor
            idPrefix={`sync-selector-${dataset.datasetCode}`}
            datasetCode={dataset.datasetCode}
            selector={target.selector}
            selectorKinds={dataset.capability.selectorKinds}
            onChange={handleSelectorChange}
          />
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 以 capability-aware 表单完成预检、确认和稳定幂等提交的同步 Dialog。 */
export function SyncCommandDialog({
  open,
  datasets,
  onClose,
  onSubmission,
}: SyncCommandDialogProps) {
  /** 防御性过滤只读资产，避免任何调用路径把它们渲染成重复警告。 */
  const manuallySyncableDatasets = useMemo(
    () => datasets.filter((dataset) => dataset.capability.manualEnabled),
    [datasets],
  );
  const model = useSyncCommandDialog({
    datasets: manuallySyncableDatasets,
    onSubmission,
  });

  /** 关闭聚焦任务，未提交草稿不会写入浏览器 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 更新强制审计原因。 */
  const handleReasonChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      model.setOperationReason(event.target.value);
    },
    [model],
  );

  return (
    <Dialog open={open} onClose={handleClose} aria-labelledby="sync-command-dialog-title">
      <DialogTitle id="sync-command-dialog-title">下发同步任务</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          {/* 每个 target 独立由所属数据集 capability 约束模式与日期字段。 */}
          {manuallySyncableDatasets.map((dataset) => (
            <SyncTargetEditor
              key={dataset.datasetCode}
              dataset={dataset}
              target={model.targets.find((target) => target.datasetCode === dataset.datasetCode)}
              onModeChange={model.setTargetMode}
              onDateFromChange={model.setDateFrom}
              onDateToChange={model.setDateTo}
              onObservationDateChange={model.setObservationDate}
              onSelectorChange={model.setTargetSelector}
            />
          ))}
          {manuallySyncableDatasets.length === 0 ? (
            <Alert severity="info">当前没有可人工同步的数据集。</Alert>
          ) : null}
          <TextField
            id="sync-command-reason"
            label="操作原因"
            value={model.reason}
            onChange={handleReasonChange}
            required
            multiline
            minRows={2}
            helperText="至少 2 个字符；原因将进入公开操作记录。"
            error={model.reason.length > 0 && model.reason.trim().length < 2}
          />
          {model.preflightErrorCode !== undefined ? (
            <Alert severity="error" icon={<ErrorOutlineIcon />}>
              预检失败：{model.preflightErrorCode}
            </Alert>
          ) : null}
          {model.submitErrorCode !== undefined ? (
            <Alert severity="error" icon={<ErrorOutlineIcon />}>
              提交失败：{model.submitErrorCode}
            </Alert>
          ) : null}
          {model.preflight !== undefined ? (
            <>
              <Divider />
              <Box component="section" aria-labelledby="sync-preflight-title">
                <Typography id="sync-preflight-title" variant="subtitle1">
                  预检结果
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  队列深度 {model.preflight.queueDepth} · 有效至 {model.preflight.expiresAt}
                </Typography>
                {/* 保持服务端预检 target 顺序，不按数据集名称重新排序。 */}
                {model.preflight.targets.map((result) => (
                  <Typography key={result.target.datasetCode} variant="body2" sx={{ mt: 1 }}>
                    {result.target.datasetCode} · {targetSelectorSummary(result.target.selector)} ·{" "}
                    {result.eligible ? "可提交" : "不可提交"} · 预计 {result.estimatedPartitions}{" "}
                    分区 / {result.estimatedProviderCalls} 次来源调用
                    {result.warnings.length > 0 ? ` · ${result.warnings.join("；")}` : ""}
                  </Typography>
                ))}
              </Box>
            </>
          ) : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={model.isPreflighting || model.isSubmitting}>
          取消
        </Button>
        {model.preflight === undefined ? (
          <Button
            variant="contained"
            onClick={model.requestPreflight}
            disabled={!model.canPreflight || model.isPreflighting}
          >
            {model.isPreflighting ? "预检中" : "执行预检"}
          </Button>
        ) : (
          <Button
            variant="contained"
            onClick={model.submit}
            disabled={!model.canSubmit || model.isSubmitting}
          >
            {model.isSubmitting ? "正在提交" : `提交同步意图（${manuallySyncableDatasets.length}）`}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
