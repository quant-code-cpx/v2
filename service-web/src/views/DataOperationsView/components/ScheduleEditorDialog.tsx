import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Typography,
  type SelectChangeEvent,
} from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";
import type { ChangeEvent } from "react";

import {
  createDataOperationIdempotencyKey,
  upsertDataSyncSchedule,
} from "../../../api/data-operations";
import { isApiError } from "../../../api/http";
import type {
  DatasetSummary,
  ScheduleFrequency,
  ScheduleTargetPolicy,
  ScheduleView,
  SubmissionReceipt,
  SyncMode,
  TargetSelector,
} from "../../../types/data-operations";
import {
  createDefaultTargetSelector,
  isTargetSelectorStructurallyReady,
} from "../utils/target-selector";
import { TargetSelectorEditor } from "./TargetSelectorEditor";

/** 从 capability 的唯一默认项取得一个已版本化计划策略。 */
function defaultPolicy(
  dataset: DatasetSummary | undefined,
  mode: Exclude<SyncMode, "DATE_RANGE">,
): ScheduleTargetPolicy | undefined {
  return dataset?.capability.scheduleTargetPolicyOptions.find(
    (option) => option.mode === mode && option.isDefault,
  )?.policy;
}

/** 从结构化频率生成一个安全、可编辑的初始每日计划。 */
function defaultFrequency(): ScheduleFrequency {
  return {
    kind: "DAILY",
    timezone: "Asia/Shanghai",
    localTime: "18:00",
    dayOfWeek: null,
    dayOfMonth: null,
    intervalMinutes: null,
    calendarCode: null,
  };
}

/** 描述计划编辑器的可用数据集、现有计划与完成回调。 */
interface ScheduleEditorDialogProps {
  scheduleId: string | undefined;
  schedules: ScheduleView[];
  datasets: DatasetSummary[];
  onClose: () => void;
  onSubmission: (receipt: SubmissionReceipt) => void;
}

/** 以服务端 capability、策略版本和乐观锁编辑自动同步计划。 */
export function ScheduleEditorDialog({
  scheduleId,
  schedules,
  datasets,
  onClose,
  onSubmission,
}: ScheduleEditorDialogProps) {
  const queryClient = useQueryClient();
  const existingSchedule = schedules.find((schedule) => schedule.scheduleId === scheduleId);
  const initialDataset =
    existingSchedule === undefined
      ? datasets.find((dataset) => dataset.capability.scheduleEligible)
      : datasets.find((dataset) => dataset.datasetCode === existingSchedule.datasetCode);
  const [datasetCode, setDatasetCode] = useState(initialDataset?.datasetCode ?? "");
  const selectedDataset = datasets.find((dataset) => dataset.datasetCode === datasetCode);
  const initialMode =
    existingSchedule?.mode ??
    selectedDataset?.capability.scheduleSupportedModes[0] ??
    "INCREMENTAL";
  const [mode, setMode] = useState<Exclude<SyncMode, "DATE_RANGE">>(initialMode);
  const [selector, setSelector] = useState<TargetSelector | undefined>(
    existingSchedule?.selector ??
      createDefaultTargetSelector(
        initialDataset?.capability.selectorKinds ?? [],
        initialDataset?.datasetCode ?? "",
      ),
  );
  const [targetPolicy, setTargetPolicy] = useState<ScheduleTargetPolicy | undefined>(
    existingSchedule?.targetPolicy ?? defaultPolicy(selectedDataset, initialMode),
  );
  const [frequency, setFrequency] = useState<ScheduleFrequency>(
    existingSchedule?.frequency ?? defaultFrequency(),
  );
  const [misfirePolicy, setMisfirePolicy] = useState<"SKIP" | "RUN_ONCE">(
    existingSchedule?.misfirePolicy ?? "RUN_ONCE",
  );
  const [coalesce, setCoalesce] = useState(existingSchedule?.coalesce ?? true);
  const [enabled, setEnabled] = useState(existingSchedule?.enabled ?? true);
  const [reason, setReason] = useState("");
  const idempotencyKeyRef = useRef<string | undefined>(undefined);
  const policyOptions = useMemo(
    () =>
      selectedDataset?.capability.scheduleTargetPolicyOptions.filter(
        (option) => option.mode === mode,
      ) ?? [],
    [mode, selectedDataset?.capability.scheduleTargetPolicyOptions],
  );
  const mutation = useMutation({
    mutationFn: async (): Promise<SubmissionReceipt> => {
      if (
        selectedDataset === undefined ||
        targetPolicy === undefined ||
        selector === undefined ||
        !isTargetSelectorStructurallyReady(selector)
      )
        throw new Error("计划能力或目标策略不可用。");
      const idempotencyKey = idempotencyKeyRef.current ?? createDataOperationIdempotencyKey();
      idempotencyKeyRef.current = idempotencyKey;
      return upsertDataSyncSchedule(
        {
          scheduleId: existingSchedule?.scheduleId ?? null,
          datasetCode: selectedDataset.datasetCode,
          mode,
          selector,
          targetPolicy,
          frequency,
          misfirePolicy,
          coalesce,
          enabled,
          expectedVersion: existingSchedule?.version ?? null,
          reason,
        },
        { idempotencyKey },
      );
    },
    /** 保存意图后刷新计划投影，随后由 submission 对账实际结果。 */
    onSuccess: (receipt) => {
      void queryClient.invalidateQueries({ queryKey: ["dataOperations"] });
      onSubmission(receipt);
    },
  });

  /** 创建时切换数据集，并把模式及策略重置到新 capability 默认项。 */
  const handleDatasetChange = useCallback(
    (event: SelectChangeEvent) => {
      const nextDatasetCode = event.target.value;
      const nextDataset = datasets.find((dataset) => dataset.datasetCode === nextDatasetCode);
      const nextMode = nextDataset?.capability.scheduleSupportedModes[0];
      setDatasetCode(nextDatasetCode);
      if (nextMode !== undefined) {
        setMode(nextMode);
        setTargetPolicy(defaultPolicy(nextDataset, nextMode));
      }
      setSelector(
        createDefaultTargetSelector(nextDataset?.capability.selectorKinds ?? [], nextDatasetCode),
      );
      idempotencyKeyRef.current = undefined;
    },
    [datasets],
  );

  /** 切换计划模式，并严格使用该模式返回的版本化策略选项。 */
  const handleModeChange = useCallback(
    (event: SelectChangeEvent) => {
      const nextMode = event.target.value as Exclude<SyncMode, "DATE_RANGE">;
      setMode(nextMode);
      setTargetPolicy(defaultPolicy(selectedDataset, nextMode));
      idempotencyKeyRef.current = undefined;
    },
    [selectedDataset],
  );

  /** 更新该计划的受限业务 selector，并使当前稳定幂等键失效。 */
  const handleSelectorChange = useCallback((nextSelector: TargetSelector) => {
    setSelector(nextSelector);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 选择服务端声明的 target policy，不创建客户端虚构策略。 */
  const handlePolicyChange = useCallback(
    (event: SelectChangeEvent) => {
      const option = policyOptions.find(
        (candidate) =>
          `${candidate.policy.policyVersion}:${candidate.policy.dateResolution}` ===
          event.target.value,
      );
      setTargetPolicy(option?.policy);
      idempotencyKeyRef.current = undefined;
    },
    [policyOptions],
  );

  /** 修改受控频率类型，并清除与该频率无关的字段。 */
  const handleFrequencyKindChange = useCallback((event: SelectChangeEvent) => {
    const kind = event.target.value as ScheduleFrequency["kind"];
    setFrequency((current) => ({
      ...current,
      kind,
      localTime: kind === "INTERVAL" ? null : (current.localTime ?? "18:00"),
      dayOfWeek: kind === "WEEKLY" ? (current.dayOfWeek ?? 1) : null,
      dayOfMonth: kind === "MONTHLY" ? (current.dayOfMonth ?? 1) : null,
      intervalMinutes: kind === "INTERVAL" ? (current.intervalMinutes ?? 60) : null,
      calendarCode: kind === "TRADING_DAY" ? (current.calendarCode ?? "CN_A_SHARE") : null,
    }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改计划本地执行时间。 */
  const handleLocalTimeChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setFrequency((current) => ({
      ...current,
      localTime: event.target.value,
    }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改频率时区。 */
  const handleTimezoneChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setFrequency((current) => ({ ...current, timezone: event.target.value }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改周计划的星期字段。 */
  const handleDayOfWeekChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setFrequency((current) => ({
      ...current,
      dayOfWeek: Number(event.target.value),
    }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改月计划的日期字段。 */
  const handleDayOfMonthChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setFrequency((current) => ({
      ...current,
      dayOfMonth: Number(event.target.value),
    }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改间隔分钟数。 */
  const handleIntervalChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setFrequency((current) => ({
      ...current,
      intervalMinutes: Number(event.target.value),
    }));
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改计划保存原因。 */
  const handleReasonChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setReason(event.target.value);
  }, []);

  /** 修改计划 enable 状态，仍走 upsert 的版本保护。 */
  const handleEnabledChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setEnabled(event.target.checked);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改错过触发的服务端策略。 */
  const handleMisfireChange = useCallback((event: SelectChangeEvent) => {
    setMisfirePolicy(event.target.value as "SKIP" | "RUN_ONCE");
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 修改是否合并连续错过的计划触发。 */
  const handleCoalesceChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    setCoalesce(event.target.checked);
    idempotencyKeyRef.current = undefined;
  }, []);

  /** 提交创建或乐观锁更新意图。 */
  const handleSubmit = useCallback(() => {
    if (
      selectedDataset !== undefined &&
      selector !== undefined &&
      targetPolicy !== undefined &&
      reason.trim().length >= 2
    )
      mutation.mutate();
  }, [mutation, reason, selectedDataset, selector, targetPolicy]);

  /** 关闭计划编辑焦点任务。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  const errorCode = isApiError(mutation.error) ? mutation.error.code : undefined;
  const isEditing = existingSchedule !== undefined;
  const selectorReady = selector !== undefined && isTargetSelectorStructurallyReady(selector);
  return (
    <Dialog
      open={scheduleId !== undefined}
      onClose={handleClose}
      aria-labelledby="schedule-editor-dialog-title"
    >
      <DialogTitle id="schedule-editor-dialog-title">
        {isEditing ? "编辑自动计划" : "新建自动计划"}
      </DialogTitle>
      <DialogContent dividers>
        {selectedDataset === undefined ? (
          <Alert severity="warning">当前目录没有可创建自动计划的数据集。</Alert>
        ) : (
          <Stack spacing={2}>
            <Alert severity="info">
              只显示 `scheduleSupportedModes` 与 `scheduleTargetPolicyOptions`；v1 不提供 DATE_RANGE
              或任意 cron。
            </Alert>
            <FormControl fullWidth disabled={isEditing}>
              <InputLabel id="schedule-dataset-label">数据集</InputLabel>
              <Select
                labelId="schedule-dataset-label"
                label="数据集"
                value={datasetCode}
                onChange={handleDatasetChange}
              >
                {/* 创建时只显示服务端允许计划的数据集，编辑时不可改绑。 */}
                {datasets
                  .filter((dataset) => dataset.capability.scheduleEligible)
                  .map((dataset) => (
                    <MenuItem key={dataset.datasetCode} value={dataset.datasetCode}>
                      {dataset.displayName} · {dataset.datasetCode}
                    </MenuItem>
                  ))}
              </Select>
            </FormControl>
            <Stack direction="row" spacing={2}>
              <FormControl sx={{ flex: 1 }}>
                <InputLabel id="schedule-mode-label">同步模式</InputLabel>
                <Select
                  labelId="schedule-mode-label"
                  label="同步模式"
                  value={mode}
                  onChange={handleModeChange}
                >
                  {selectedDataset.capability.scheduleSupportedModes.map((availableMode) => (
                    <MenuItem key={availableMode} value={availableMode}>
                      {availableMode}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <FormControl sx={{ flex: 1 }}>
                <InputLabel id="schedule-policy-label">目标策略</InputLabel>
                <Select
                  labelId="schedule-policy-label"
                  label="目标策略"
                  value={
                    targetPolicy === undefined
                      ? ""
                      : `${targetPolicy.policyVersion}:${targetPolicy.dateResolution}`
                  }
                  onChange={handlePolicyChange}
                >
                  {policyOptions.map((option) => (
                    <MenuItem
                      key={`${option.policy.policyVersion}:${option.policy.dateResolution}`}
                      value={`${option.policy.policyVersion}:${option.policy.dateResolution}`}
                    >
                      {option.policy.dateResolution} · policy v{option.policy.policyVersion}
                      {option.isDefault ? "（默认）" : ""}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Stack>
            {selector !== undefined ? (
              <>
                <TargetSelectorEditor
                  idPrefix="schedule-selector"
                  datasetCode={selectedDataset.datasetCode}
                  selector={selector}
                  selectorKinds={selectedDataset.capability.selectorKinds}
                  onChange={handleSelectorChange}
                  disabled={mutation.isPending}
                />
                {!selectorReady ? (
                  <Alert severity="warning">请补全当前业务范围的必填字段后再保存计划。</Alert>
                ) : null}
              </>
            ) : (
              <Alert severity="warning">该数据集没有可用的受限业务范围，不能保存计划。</Alert>
            )}
            <Stack direction="row" spacing={2}>
              <FormControl sx={{ width: 180 }}>
                <InputLabel id="schedule-frequency-label">频率</InputLabel>
                <Select
                  labelId="schedule-frequency-label"
                  label="频率"
                  value={frequency.kind}
                  onChange={handleFrequencyKindChange}
                >
                  <MenuItem value="TRADING_DAY">交易日</MenuItem>
                  <MenuItem value="DAILY">每日</MenuItem>
                  <MenuItem value="WEEKLY">每周</MenuItem>
                  <MenuItem value="MONTHLY">每月</MenuItem>
                  <MenuItem value="INTERVAL">固定间隔</MenuItem>
                </Select>
              </FormControl>
              <TextField
                label="时区"
                value={frequency.timezone}
                onChange={handleTimezoneChange}
                sx={{ flex: 1 }}
              />
              {frequency.kind !== "INTERVAL" ? (
                <TextField
                  label="本地时间"
                  type="time"
                  value={frequency.localTime ?? ""}
                  onChange={handleLocalTimeChange}
                  slotProps={{ inputLabel: { shrink: true } }}
                />
              ) : null}
              {frequency.kind === "WEEKLY" ? (
                <TextField
                  label="星期（1-7）"
                  type="number"
                  value={frequency.dayOfWeek ?? 1}
                  onChange={handleDayOfWeekChange}
                  sx={{ width: 140 }}
                />
              ) : null}
              {frequency.kind === "MONTHLY" ? (
                <TextField
                  label="日期（1-31）"
                  type="number"
                  value={frequency.dayOfMonth ?? 1}
                  onChange={handleDayOfMonthChange}
                  sx={{ width: 140 }}
                />
              ) : null}
              {frequency.kind === "INTERVAL" ? (
                <TextField
                  label="间隔分钟"
                  type="number"
                  value={frequency.intervalMinutes ?? 60}
                  onChange={handleIntervalChange}
                  sx={{ width: 160 }}
                />
              ) : null}
            </Stack>
            <Stack direction="row" spacing={2} alignItems="center">
              <FormControl sx={{ width: 180 }}>
                <InputLabel id="schedule-misfire-label">错过触发</InputLabel>
                <Select
                  labelId="schedule-misfire-label"
                  label="错过触发"
                  value={misfirePolicy}
                  onChange={handleMisfireChange}
                >
                  <MenuItem value="SKIP">跳过</MenuItem>
                  <MenuItem value="RUN_ONCE">补跑一次</MenuItem>
                </Select>
              </FormControl>
              <Stack direction="row" spacing={0.5} alignItems="center">
                <Switch checked={coalesce} onChange={handleCoalesceChange} />
                <Typography variant="body2">合并连续错过</Typography>
              </Stack>
              <Stack direction="row" spacing={0.5} alignItems="center">
                <Switch checked={enabled} onChange={handleEnabledChange} />
                <Typography variant="body2">保存后启用</Typography>
              </Stack>
            </Stack>
            <TextField
              label="操作原因"
              value={reason}
              onChange={handleReasonChange}
              required
              multiline
              minRows={2}
              error={reason.length > 0 && reason.trim().length < 2}
              helperText="至少 2 个字符；计划更新使用服务端乐观版本保护。"
            />
            {errorCode !== undefined ? <Alert severity="error">提交失败：{errorCode}</Alert> : null}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose} disabled={mutation.isPending}>
          取消
        </Button>
        <Button
          variant="contained"
          onClick={handleSubmit}
          disabled={
            selectedDataset === undefined ||
            !selectorReady ||
            targetPolicy === undefined ||
            reason.trim().length < 2 ||
            mutation.isPending
          }
        >
          {mutation.isPending ? "正在提交" : "提交计划意图"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
