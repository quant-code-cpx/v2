import {
  HealthAndSafetyOutlined as HealthAndSafetyOutlinedIcon,
  PlayArrowOutlined as PlayArrowOutlinedIcon,
  SearchOutlined as SearchOutlinedIcon,
} from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  Chip,
  FormControl,
  InputLabel,
  InputAdornment,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
  type SelectChangeEvent,
} from "@mui/material";
import { useCallback, useMemo, useState } from "react";
import type { ChangeEvent } from "react";

import type {
  DatasetPage,
  DatasetSearchRequest,
  DatasetSummary,
} from "../../../types/data-operations";
import {
  formatDataOperationsDate,
  formatDataOperationsDateTime,
  freshnessStatusLabel,
  healthStatusLabel,
  runStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";
import { CursorPager } from "./CursorPager";
import { DataOperationsTableEmptyState } from "./DataOperationsTableEmptyState";

/** 描述资产目录的远程数据、交互和主动操作入口。 */
interface DatasetCatalogPanelProps {
  data: DatasetPage | undefined;
  filters: DatasetSearchRequest;
  isLoading: boolean;
  isError: boolean;
  canWrite: boolean;
  onFiltersChange: (update: Partial<DatasetSearchRequest>) => void;
  onPageChange: (cursor: string | undefined) => void;
  onRefresh: () => void;
  onOpenDataset: (datasetCode: string) => void;
  onSync: (datasets: DatasetSummary[]) => void;
  onHealthCheck: (datasets: DatasetSummary[]) => void;
}

/** 展示可分享筛选、来源分层、时间语义与临时批量选择的数据资产目录。 */
export function DatasetCatalogPanel({
  data,
  filters,
  isLoading,
  isError,
  canWrite,
  onFiltersChange,
  onPageChange,
  onRefresh,
  onOpenDataset,
  onSync,
  onHealthCheck,
}: DatasetCatalogPanelProps) {
  const [selectedDatasetCodes, setSelectedDatasetCodes] = useState<Set<string>>(() => new Set());
  const selectedDatasets = useMemo(
    () => data?.items.filter((item) => selectedDatasetCodes.has(item.datasetCode)) ?? [],
    [data?.items, selectedDatasetCodes],
  );
  const syncableSelectedDatasets = useMemo(
    () => selectedDatasets.filter((item) => item.capability.manualEnabled),
    [selectedDatasets],
  );

  /** 将目录关键词写入 URL 驱动筛选，并重置服务端 cursor。 */
  const handleQueryChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onFiltersChange({ query: event.target.value.length === 0 ? undefined : event.target.value });
    },
    [onFiltersChange],
  );

  /** 更新单个供应商筛选，真实上游仍由服务端单独返回。 */
  const handleProviderChange = useCallback(
    (event: SelectChangeEvent) => {
      const value = event.target.value;
      onFiltersChange({ providers: value.length === 0 ? undefined : [value] });
    },
    [onFiltersChange],
  );

  /** 切换本页数据集的临时批量选择，不把选择集合放入 URL。 */
  const handleToggleDataset = useCallback((datasetCode: string) => {
    setSelectedDatasetCodes((current) => {
      const next = new Set(current);
      if (next.has(datasetCode)) {
        next.delete(datasetCode);
      } else {
        next.add(datasetCode);
      }
      return next;
    });
  }, []);

  /** 清空当前页临时选择，保留后端筛选和 cursor。 */
  const handleClearSelection = useCallback(() => {
    setSelectedDatasetCodes(new Set());
  }, []);

  /** 打开当前选择的数据集同步预检表单。 */
  const handleBatchSync = useCallback(() => {
    onSync(syncableSelectedDatasets);
  }, [onSync, syncableSelectedDatasets]);

  /** 打开当前选择的主动健康检查表单。 */
  const handleBatchHealthCheck = useCallback(() => {
    onHealthCheck(selectedDatasets);
  }, [onHealthCheck, selectedDatasets]);

  return (
    <Stack spacing={2} component="section" aria-labelledby="dataset-catalog-title">
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end">
        <Typography id="dataset-catalog-title" variant="h4">
          数据资产
        </Typography>
      </Stack>
      {isError && data === undefined ? (
        <Alert
          severity="error"
          action={
            <Button color="inherit" onClick={onRefresh}>
              重试
            </Button>
          }
        >
          无法读取数据资产目录。请稍后重试；不会把读取失败显示为空目录。
        </Alert>
      ) : null}
      <Card sx={{ overflow: "hidden" }}>
        <CardContent sx={{ borderBottom: 1, borderColor: "divider" }}>
          <Stack direction="row" spacing={2} alignItems="center">
            <TextField
              label="搜索数据集"
              value={filters.query ?? ""}
              onChange={handleQueryChange}
              placeholder="名称、代码或领域"
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchOutlinedIcon fontSize="small" color="disabled" />
                    </InputAdornment>
                  ),
                },
              }}
              sx={{ width: 320 }}
            />
            <FormControl sx={{ width: 220 }}>
              <InputLabel id="dataset-provider-filter-label">供应商</InputLabel>
              <Select
                labelId="dataset-provider-filter-label"
                label="供应商"
                value={filters.providers?.[0] ?? ""}
                onChange={handleProviderChange}
              >
                <MenuItem value="">全部供应商</MenuItem>
                {/* 目录来源可扩展，当前筛选值不把 Provider 名称硬编码为能力。 */}
                {Array.from(
                  new Set(
                    data?.items.flatMap((item) =>
                      item.sourceBindings.map((source) => source.providerId),
                    ) ?? [],
                  ),
                ).map((providerId) => (
                  <MenuItem key={providerId} value={providerId}>
                    {providerId}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            {canWrite && selectedDatasets.length > 0 ? (
              <Stack direction="row" spacing={1} sx={{ ml: "auto" }}>
                <Button
                  startIcon={<HealthAndSafetyOutlinedIcon />}
                  onClick={handleBatchHealthCheck}
                >
                  健康检查（{selectedDatasets.length}）
                </Button>
                {syncableSelectedDatasets.length > 0 ? (
                  <Button
                    variant="contained"
                    startIcon={<PlayArrowOutlinedIcon />}
                    onClick={handleBatchSync}
                  >
                    批量同步（{syncableSelectedDatasets.length}）
                  </Button>
                ) : null}
                <Button onClick={handleClearSelection}>清空</Button>
              </Stack>
            ) : null}
          </Stack>
        </CardContent>
        <TableContainer sx={{ overflowX: "auto" }}>
          <Table size="small" aria-label="数据资产目录" sx={{ minWidth: 1280 }}>
            <TableHead>
              <TableRow>
                {canWrite ? <TableCell padding="checkbox" aria-label="批量选择" /> : null}
                <TableCell sx={{ width: 230, whiteSpace: "nowrap" }}>数据集</TableCell>
                <TableCell sx={{ width: 280, whiteSpace: "nowrap" }}>供应商 / 上游来源</TableCell>
                <TableCell sx={{ width: 170, whiteSpace: "nowrap" }}>数据时点 / 发布</TableCell>
                <TableCell sx={{ width: 210, whiteSpace: "nowrap" }}>最近尝试 / 最近成功</TableCell>
                <TableCell sx={{ width: 180, whiteSpace: "nowrap" }}>新鲜度 / 健康</TableCell>
                <TableCell align="right" sx={{ width: 120, whiteSpace: "nowrap" }}>
                  操作
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {/* 保持服务端分页返回顺序，逐项渲染当前目录页。 */}
              {data !== undefined && data.items.length === 0 ? (
                <DataOperationsTableEmptyState
                  colSpan={canWrite ? 7 : 6}
                  title="没有匹配数据资产"
                  description="调整搜索或供应商筛选后重试。"
                />
              ) : null}
              {data?.items.map((dataset) => {
                const primarySource = dataset.sourceBindings[0];
                const isSelected = selectedDatasetCodes.has(dataset.datasetCode);
                return (
                  <TableRow key={dataset.datasetCode} hover selected={isSelected}>
                    {canWrite ? (
                      <TableCell padding="checkbox">
                        <Checkbox
                          checked={isSelected}
                          onChange={() => handleToggleDataset(dataset.datasetCode)}
                          inputProps={{ "aria-label": `选择 ${dataset.displayName}` }}
                        />
                      </TableCell>
                    ) : null}
                    <TableCell>
                      <Typography fontWeight={700}>{dataset.displayName}</Typography>
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ fontFamily: "monospace" }}
                      >
                        {dataset.datasetCode}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {primarySource?.providerId ?? "未声明"}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {primarySource === undefined
                          ? "未声明上游来源"
                          : `${primarySource.upstreamSource} · ${primarySource.sourceDataset}`}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {dataset.timing.dataAsOfLabel}{" "}
                        {formatDataOperationsDate(dataset.timing.dataAsOf)}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        最近发布 {formatDataOperationsDateTime(dataset.timing.lastPublishedAt)}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2">
                        {dataset.timing.lastAttemptStatus === null
                          ? "从未尝试"
                          : `${runStatusLabel(dataset.timing.lastAttemptStatus)} · ${formatDataOperationsDateTime(dataset.timing.lastAttemptStartedAt)}`}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        最近成功 {formatDataOperationsDateTime(dataset.timing.lastSuccessAt)}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Stack
                        direction="row"
                        spacing={0.75}
                        alignItems="center"
                        flexWrap="wrap"
                        useFlexGap
                      >
                        <Chip
                          size="small"
                          color={statusChipColor(dataset.timing.freshnessStatus)}
                          label={freshnessStatusLabel(dataset.timing.freshnessStatus)}
                        />
                        <Chip
                          size="small"
                          color={statusChipColor(dataset.healthSummary.status)}
                          label={healthStatusLabel(dataset.healthSummary.status)}
                        />
                      </Stack>
                      <Typography variant="caption" color="text.secondary">
                        观测：{dataset.observationState}
                      </Typography>
                    </TableCell>
                    <TableCell align="right">
                      <Stack direction="row" justifyContent="flex-end" spacing={0.5}>
                        <Button size="small" onClick={() => onOpenDataset(dataset.datasetCode)}>
                          详情
                        </Button>
                        {canWrite ? (
                          <Button
                            size="small"
                            onClick={() => onSync([dataset])}
                            disabled={!dataset.capability.manualEnabled}
                          >
                            同步
                          </Button>
                        ) : null}
                      </Stack>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
        {data !== undefined ? (
          <Box sx={{ px: 3, py: 2, borderTop: 1, borderColor: "divider" }}>
            <Typography variant="body2" color="text.secondary">
              共 {data.totalEstimate} 个数据资产
            </Typography>
          </Box>
        ) : null}
        {data !== undefined ? (
          <CursorPager
            currentCursor={filters.cursor}
            nextCursor={data.nextCursor}
            isLoading={isLoading}
            onPageChange={onPageChange}
          />
        ) : null}
      </Card>
    </Stack>
  );
}
