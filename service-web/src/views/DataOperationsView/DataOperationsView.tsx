import { AddOutlined as AddOutlinedIcon } from "@mui/icons-material";
import { Alert, Box, Button, Skeleton, Stack, Tab, Tabs, Typography } from "@mui/material";
import { useCallback, useState } from "react";
import type { SyntheticEvent } from "react";

import { useFeedback } from "../../components/FeedbackProvider";
import type {
  CommandActionTarget,
  DatasetSummary,
  ScheduleView,
  SubmissionReceipt,
} from "../../types/data-operations";
import { CommandActionDialog } from "./components/CommandActionDialog";
import { CommandDetailDrawer } from "./components/CommandDetailDrawer";
import { DataOperationAuditPanel } from "./components/DataOperationAuditPanel";
import { DataOperationsRunBar } from "./components/DataOperationsRunBar";
import { DatasetCatalogPanel } from "./components/DatasetCatalogPanel";
import { DatasetDetailDrawer } from "./components/DatasetDetailDrawer";
import { HealthCheckDetailDrawer } from "./components/HealthCheckDetailDrawer";
import { HealthCheckDialog } from "./components/HealthCheckDialog";
import { HealthEvaluationDetailDrawer } from "./components/HealthEvaluationDetailDrawer";
import { HealthEvaluationsPanel } from "./components/HealthEvaluationsPanel";
import { RunDetailDrawer } from "./components/RunDetailDrawer";
import { ScheduleEditorDialog } from "./components/ScheduleEditorDialog";
import { ScheduleEnableDialog } from "./components/ScheduleEnableDialog";
import { SchedulePanel } from "./components/SchedulePanel";
import { SubmissionTrackerDrawer } from "./components/SubmissionTrackerDrawer";
import { SyncCommandDialog } from "./components/SyncCommandDialog";
import { TaskQueuePanel } from "./components/TaskQueuePanel";
import { useDataOperationsPage } from "./hooks/useDataOperationsPage";
import { dataOperationsTabs } from "./utils/data-operations-presentation";
import type { DataOperationsTab } from "./utils/data-operations-presentation";

/** 描述页面内临时取消或重试 Dialog 的明确资源作用域。 */
interface CommandActionDialogState {
  action: "cancel" | "retry";
  target: CommandActionTarget;
}

/** 组合数据资产、串行任务、健康、自动计划和操作记录的受保护桌面工作台。 */
export function DataOperationsView() {
  const model = useDataOperationsPage();
  const feedback = useFeedback();
  const [syncDatasets, setSyncDatasets] = useState<DatasetSummary[] | undefined>();
  const [healthCheckDatasets, setHealthCheckDatasets] = useState<DatasetSummary[] | undefined>();
  const [commandAction, setCommandAction] = useState<CommandActionDialogState | undefined>();
  const [scheduleEnableTarget, setScheduleEnableTarget] = useState<ScheduleView | undefined>();

  /** 切换 URL 驱动的任务型 Tab，不携带未提交表单状态。 */
  const handleTabChange = useCallback(
    (_event: SyntheticEvent, value: DataOperationsTab) => {
      model.setTab(value);
    },
    [model],
  );

  /** 打开 capability-aware 同步 Dialog，仅接受当前目录的服务端摘要。 */
  const handleSync = useCallback(
    (datasets: DatasetSummary[]) => {
      if (!model.canWrite) {
        feedback.error("当前角色只能查看数据运维信息。");
        return;
      }
      const manuallySyncableDatasets = datasets.filter(
        (dataset) => dataset.capability.manualEnabled,
      );
      if (manuallySyncableDatasets.length === 0) {
        feedback.info("请至少选择一个支持人工同步的数据集。");
        return;
      }
      // 目录也包含只读或仅建模资产；它们不能进入同步表单，更不能逐条制造告警。
      setSyncDatasets(manuallySyncableDatasets);
    },
    [feedback, model.canWrite],
  );

  /** 打开批量主动健康检查 Dialog，并保留每个 target 的独立版本输入。 */
  const handleHealthCheck = useCallback(
    (datasets: DatasetSummary[]) => {
      if (!model.canWrite) {
        feedback.error("当前角色只能查看数据运维信息。");
        return;
      }
      if (datasets.length === 0) {
        feedback.info("请至少选择一个数据集进行健康检查。");
        return;
      }
      setHealthCheckDatasets(datasets);
    },
    [feedback, model.canWrite],
  );

  /** 从健康 Tab 以当前目录页作为可选 target 打开主动检查。 */
  const handleStartHealthCheck = useCallback(() => {
    const datasets = model.catalogQuery.data?.items ?? [];
    if (datasets.length === 0) {
      feedback.info("请先加载至少一个数据集，再发起主动健康检查。");
      return;
    }
    handleHealthCheck(datasets);
  }, [feedback, handleHealthCheck, model.catalogQuery.data?.items]);

  /** 打开显式 COMMAND 或 RUN 作用域的取消、重试确认 Dialog。 */
  const handleCommandAction = useCallback(
    (action: "cancel" | "retry", target: CommandActionTarget) => {
      if (model.canWrite) setCommandAction({ action, target });
    },
    [model.canWrite],
  );

  /** 处理首次写回执：PENDING 只表示意图持久化，并立即进入 submission 对账。 */
  const handleSubmission = useCallback(
    (receipt: SubmissionReceipt) => {
      setSyncDatasets(undefined);
      setHealthCheckDatasets(undefined);
      setCommandAction(undefined);
      setScheduleEnableTarget(undefined);
      if (receipt.deliveryStatus === "PENDING") {
        feedback.info("已提交，等待同步服务受理。");
      } else if (receipt.deliveryStatus === "ACCEPTED") {
        feedback.info("同步服务已受理，正在对账权威资源。");
      } else {
        feedback.error("操作已记录，但投递未完成；请查看提交意图。");
      }
      model.openSubmission(receipt.submissionId);
    },
    [feedback, model],
  );

  /** 根据 submission 接受后的权威资源类型打开对应详情，不从本地状态猜测。 */
  const handleOpenAuthorityResource = useCallback(
    (resourceType: "COMMAND" | "RUN" | "HEALTH_CHECK" | "SCHEDULE", resourceId: string) => {
      if (resourceType === "COMMAND") {
        model.openCommand(resourceId);
      } else if (resourceType === "RUN") {
        model.openRun(resourceId);
      } else if (resourceType === "HEALTH_CHECK") {
        model.openHealthCheck(resourceId);
      } else {
        model.setTab("schedules");
        model.openSchedule(resourceId);
      }
    },
    [model],
  );

  /** 关闭同步 Dialog 的临时 target 草稿。 */
  const handleCloseSync = useCallback(() => {
    setSyncDatasets(undefined);
  }, []);

  /** 关闭主动健康检查 Dialog 的临时 target 草稿。 */
  const handleCloseHealthCheck = useCallback(() => {
    setHealthCheckDatasets(undefined);
  }, []);

  /** 关闭取消或重试 Dialog。 */
  const handleCloseCommandAction = useCallback(() => {
    setCommandAction(undefined);
  }, []);

  /** 关闭计划启停 Dialog。 */
  const handleCloseScheduleEnable = useCallback(() => {
    setScheduleEnableTarget(undefined);
  }, []);

  /** 在计划列表中打开带原因和乐观版本的启停确认。 */
  const handleSetScheduleEnabled = useCallback((schedule: ScheduleView) => {
    setScheduleEnableTarget(schedule);
  }, []);

  if (model.user === undefined) {
    return (
      <Stack spacing={3} aria-label="正在恢复数据运维会话">
        <Skeleton variant="text" width={180} height={48} />
        <Skeleton variant="rounded" height={112} />
        <Skeleton variant="rounded" height={480} />
      </Stack>
    );
  }

  if (!model.canRead) {
    return <Alert severity="error">当前账号无权读取数据运维控制面。</Alert>;
  }

  return (
    <Stack spacing={3}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Typography component="h1" variant="h3">
            数据运维
          </Typography>
        </Box>
        {model.canWrite ? (
          <Button
            variant="contained"
            startIcon={<AddOutlinedIcon />}
            onClick={() => handleSync(model.catalogQuery.data?.items ?? [])}
          >
            下发同步任务
          </Button>
        ) : null}
      </Stack>
      {!model.canWrite ? (
        <Alert severity="info">管理员首版只读；同步、健康检查和计划变更仅超级管理员可用。</Alert>
      ) : null}
      <DataOperationsRunBar
        overview={model.overviewQuery.data}
        isLoading={model.overviewQuery.isPending}
        isError={model.overviewQuery.isError}
        onRetry={model.refresh}
      />
      <Tabs
        value={model.state.tab}
        onChange={handleTabChange}
        aria-label="数据运维工作区"
        sx={{ "& .MuiTabs-flexContainer": { gap: 3 } }}
      >
        {/* 保持任务型 Tab 顺序，与既有桌面原型一致。 */}
        {dataOperationsTabs.map((tab) => (
          <Tab
            key={tab}
            value={tab}
            label={
              {
                datasets: "数据资产",
                runs: "同步任务",
                health: "健康度",
                schedules: "自动计划",
                operations: "操作记录",
              }[tab]
            }
          />
        ))}
      </Tabs>
      {model.state.tab === "datasets" ? (
        <DatasetCatalogPanel
          data={model.catalogQuery.data}
          filters={model.state.catalog}
          isLoading={model.catalogQuery.isPending}
          isError={model.catalogQuery.isError}
          canWrite={model.canWrite}
          onFiltersChange={model.updateCatalog}
          onPageChange={(cursor) => model.updateCatalog({ cursor })}
          onRefresh={model.refresh}
          onOpenDataset={model.openDataset}
          onSync={handleSync}
          onHealthCheck={handleHealthCheck}
        />
      ) : null}
      {model.state.tab === "runs" ? (
        <TaskQueuePanel
          data={model.runsQuery.data}
          isLoading={model.runsQuery.isPending}
          isError={model.runsQuery.isError}
          canWrite={model.canWrite}
          cursor={model.state.runCursor}
          onPageChange={model.setRunCursor}
          onRefresh={model.refresh}
          onOpenRun={model.openRun}
          onOpenCommand={model.openCommand}
          onAction={handleCommandAction}
        />
      ) : null}
      {model.state.tab === "health" ? (
        <HealthEvaluationsPanel
          data={model.healthQuery.data}
          isLoading={model.healthQuery.isPending}
          isError={model.healthQuery.isError}
          canWrite={model.canWrite}
          cursor={model.state.healthCursor}
          onPageChange={model.setHealthCursor}
          onRefresh={model.refresh}
          onOpenEvaluation={model.openEvaluation}
          onOpenHealthCheck={model.openHealthCheck}
          onStartHealthCheck={handleStartHealthCheck}
        />
      ) : null}
      {model.state.tab === "schedules" ? (
        <SchedulePanel
          data={model.schedulesQuery.data}
          isLoading={model.schedulesQuery.isPending}
          isError={model.schedulesQuery.isError}
          canWrite={model.canWrite}
          cursor={model.state.scheduleCursor}
          onPageChange={model.setScheduleCursor}
          onRefresh={model.refresh}
          onOpenEditor={model.openSchedule}
          onSetEnabled={handleSetScheduleEnabled}
        />
      ) : null}
      {model.state.tab === "operations" ? (
        <DataOperationAuditPanel
          data={model.operationsQuery.data}
          isLoading={model.operationsQuery.isPending}
          isError={model.operationsQuery.isError}
          cursor={model.state.operationCursor}
          onPageChange={model.setOperationCursor}
          onRefresh={model.refresh}
        />
      ) : null}
      <DatasetDetailDrawer
        key={model.state.datasetCode}
        datasetCode={model.state.datasetCode}
        canWrite={model.canWrite}
        onClose={model.closeDetails}
        onSync={handleSync}
        onHealthCheck={handleHealthCheck}
      />
      <RunDetailDrawer
        key={model.state.runId}
        runId={model.state.runId}
        canWrite={model.canWrite}
        onClose={model.closeDetails}
        onAction={handleCommandAction}
      />
      <CommandDetailDrawer
        key={model.state.commandId}
        commandId={model.state.commandId}
        canWrite={model.canWrite}
        onClose={model.closeDetails}
        onOpenRun={model.openRun}
        onAction={handleCommandAction}
      />
      <HealthCheckDetailDrawer
        key={model.state.healthCheckId}
        healthCheckId={model.state.healthCheckId}
        onClose={model.closeDetails}
        onOpenEvaluation={model.openEvaluation}
      />
      <HealthEvaluationDetailDrawer
        key={model.state.evaluationId}
        evaluationId={model.state.evaluationId}
        onClose={model.closeDetails}
      />
      <SubmissionTrackerDrawer
        key={model.state.submissionId}
        submissionId={model.state.submissionId}
        onClose={model.closeDetails}
        onOpenResource={handleOpenAuthorityResource}
      />
      {model.state.scheduleId !== undefined ? (
        <ScheduleEditorDialog
          key={model.state.scheduleId}
          scheduleId={model.state.scheduleId}
          schedules={model.schedulesQuery.data?.items ?? []}
          datasets={model.catalogQuery.data?.items ?? []}
          onClose={model.closeDetails}
          onSubmission={handleSubmission}
        />
      ) : null}
      {syncDatasets !== undefined ? (
        <SyncCommandDialog
          open
          datasets={syncDatasets}
          onClose={handleCloseSync}
          onSubmission={handleSubmission}
        />
      ) : null}
      {healthCheckDatasets !== undefined ? (
        <HealthCheckDialog
          open
          datasets={model.catalogQuery.data?.items ?? healthCheckDatasets}
          initialDatasets={healthCheckDatasets}
          onClose={handleCloseHealthCheck}
          onSubmission={handleSubmission}
        />
      ) : null}
      <CommandActionDialog
        action={commandAction?.action}
        target={commandAction?.target}
        onClose={handleCloseCommandAction}
        onSubmission={handleSubmission}
      />
      <ScheduleEnableDialog
        schedule={scheduleEnableTarget}
        onClose={handleCloseScheduleEnable}
        onSubmission={handleSubmission}
      />
    </Stack>
  );
}
