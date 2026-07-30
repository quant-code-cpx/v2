import {
  HealthAndSafetyOutlined as HealthAndSafetyOutlinedIcon,
  PlayArrowOutlined as PlayArrowOutlinedIcon,
} from "@mui/icons-material";
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

import { operationalDatasetDetailQueryOptions } from "../../../api/data-operations";
import type { DatasetSummary } from "../../../types/data-operations";
import {
  errorSummaryLabel,
  formatDataOperationsDate,
  formatDataOperationsDateTime,
  freshnessStatusLabel,
  healthStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";

/** 描述数据资产详情 Drawer 所需的路由状态与写操作入口。 */
interface DatasetDetailDrawerProps {
  datasetCode: string | undefined;
  canWrite: boolean;
  onClose: () => void;
  onSync: (datasets: DatasetSummary[]) => void;
  onHealthCheck: (datasets: DatasetSummary[]) => void;
}

/** 展示一个数据集的二级来源、时间、健康、计划和安全主动操作入口。 */
export function DatasetDetailDrawer({
  datasetCode,
  canWrite,
  onClose,
  onSync,
  onHealthCheck,
}: DatasetDetailDrawerProps) {
  const detailQuery = useQuery({
    ...operationalDatasetDetailQueryOptions(datasetCode ?? ""),
    enabled: datasetCode !== undefined,
  });

  /** 关闭 Drawer 并由父页面清理 URL 中的详情标识。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 使用当前服务端详情摘要打开单数据集同步预检。 */
  const handleSync = useCallback(() => {
    if (detailQuery.data !== undefined) {
      onSync([detailQuery.data.summary]);
    }
  }, [detailQuery.data, onSync]);

  /** 使用当前服务端详情摘要打开单数据集主动健康检查。 */
  const handleHealthCheck = useCallback(() => {
    if (detailQuery.data !== undefined) {
      onHealthCheck([detailQuery.data.summary]);
    }
  }, [detailQuery.data, onHealthCheck]);

  return (
    <Drawer
      anchor="right"
      open={datasetCode !== undefined}
      onClose={handleClose}
      aria-labelledby="dataset-detail-title"
    >
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 3, minHeight: "100%" }}>
        {detailQuery.isPending ? (
          <Skeleton variant="rounded" height={480} aria-label="正在加载数据集详情" />
        ) : null}
        {detailQuery.isError ? (
          <Alert
            severity="error"
            action={
              <Button color="inherit" onClick={() => void detailQuery.refetch()}>
                重试
              </Button>
            }
          >
            无法读取数据集详情；目录中的旧摘要不会被改写。
          </Alert>
        ) : null}
        {detailQuery.data !== undefined ? (
          <>
            <Box>
              <Typography id="dataset-detail-title" variant="h5">
                {detailQuery.data.summary.displayName}
              </Typography>
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ fontFamily: "monospace", mt: 0.5 }}
              >
                {detailQuery.data.summary.datasetCode}
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                {detailQuery.data.description}
              </Typography>
            </Box>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Chip label={detailQuery.data.summary.availability} />
              <Chip label={detailQuery.data.summary.observationState} />
              <Chip
                color={statusChipColor(detailQuery.data.summary.timing.freshnessStatus)}
                label={freshnessStatusLabel(detailQuery.data.summary.timing.freshnessStatus)}
              />
              <Chip
                color={statusChipColor(detailQuery.data.summary.healthSummary.status)}
                label={healthStatusLabel(detailQuery.data.summary.healthSummary.status)}
              />
            </Stack>
            <Divider />
            <Box component="section" aria-labelledby="dataset-detail-source-title">
              <Typography id="dataset-detail-source-title" variant="subtitle1" sx={{ mb: 1 }}>
                当前来源血缘
              </Typography>
              <List dense disablePadding>
                {/* 来源绑定由服务端注册表提供，Provider 与真实 upstream 分开呈现。 */}
                {detailQuery.data.summary.sourceBindings.map((source) => (
                  <ListItem
                    key={`${source.providerId}-${source.upstreamSource}-${source.sourceDataset}`}
                    disableGutters
                  >
                    <ListItemText
                      primary={`${source.providerId} / ${source.upstreamSource}`}
                      secondary={`${source.sourceDataset} · ${source.adapterId} · ${source.methodologyCode} v${source.methodologyVersion}`}
                    />
                  </ListItem>
                ))}
              </List>
            </Box>
            <Divider />
            <Box component="section" aria-labelledby="dataset-detail-timing-title">
              <Typography id="dataset-detail-timing-title" variant="subtitle1" sx={{ mb: 1 }}>
                新鲜度与发布
              </Typography>
              <Stack spacing={1}>
                <Typography variant="body2">
                  数据时点：{detailQuery.data.summary.timing.dataAsOfLabel}{" "}
                  {formatDataOperationsDate(detailQuery.data.summary.timing.dataAsOf)}
                </Typography>
                <Typography variant="body2">
                  最近尝试开始：
                  {formatDataOperationsDateTime(
                    detailQuery.data.summary.timing.lastAttemptStartedAt,
                  )}
                </Typography>
                <Typography variant="body2">
                  最近尝试结束：
                  {detailQuery.data.summary.timing.lastAttemptFinishedAt === null
                    ? "尚未结束"
                    : formatDataOperationsDateTime(
                        detailQuery.data.summary.timing.lastAttemptFinishedAt,
                      )}
                </Typography>
                <Typography variant="body2">
                  最近成功：
                  {formatDataOperationsDateTime(detailQuery.data.summary.timing.lastSuccessAt)}
                </Typography>
                <Typography variant="body2">
                  最近发布：
                  {formatDataOperationsDateTime(detailQuery.data.summary.timing.lastPublishedAt)}
                </Typography>
                <Typography variant="body2">
                  服务端新鲜度：
                  {freshnessStatusLabel(detailQuery.data.summary.timing.freshnessStatus)}
                  {detailQuery.data.summary.timing.freshnessLagValue === null
                    ? ""
                    : ` · 落后 ${detailQuery.data.summary.timing.freshnessLagValue} ${detailQuery.data.summary.timing.freshnessLagUnit ?? ""}`}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  原因：{detailQuery.data.summary.timing.freshnessReasonCode ?? "—"} · 计算于{" "}
                  {formatDataOperationsDateTime(
                    detailQuery.data.summary.timing.freshnessEvaluatedAt,
                  )}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {detailQuery.data.freshnessPolicy === null
                    ? "模型数据：新鲜度不适用。"
                    : `策略：${detailQuery.data.freshnessPolicy.timezone} · 预警 ${detailQuery.data.freshnessPolicy.warnAfterMinutes} 分钟 · 严重 ${detailQuery.data.freshnessPolicy.criticalAfterMinutes} 分钟`}
                </Typography>
              </Stack>
            </Box>
            <Divider />
            <Box component="section" aria-labelledby="dataset-detail-health-title">
              <Typography id="dataset-detail-health-title" variant="subtitle1" sx={{ mb: 1 }}>
                发布后健康
              </Typography>
              <Typography variant="body2">
                {healthStatusLabel(detailQuery.data.summary.healthSummary.status)} · 开放问题{" "}
                {detailQuery.data.summary.healthSummary.openIssueCount} · 严重{" "}
                {detailQuery.data.summary.healthSummary.criticalCount}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                发布后严重只标记既有版本，不撤销已有 publication。最近错误：
                {errorSummaryLabel(detailQuery.data.latestError)}
              </Typography>
            </Box>
            {canWrite ? (
              <Stack direction="row" spacing={1} sx={{ mt: "auto" }}>
                <Button
                  variant="contained"
                  startIcon={<PlayArrowOutlinedIcon />}
                  onClick={handleSync}
                  disabled={!detailQuery.data.summary.capability.manualEnabled}
                >
                  下发同步
                </Button>
                <Button startIcon={<HealthAndSafetyOutlinedIcon />} onClick={handleHealthCheck}>
                  主动健康检查
                </Button>
              </Stack>
            ) : null}
          </>
        ) : null}
      </Box>
    </Drawer>
  );
}
