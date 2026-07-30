import { HealthAndSafetyOutlined as HealthAndSafetyOutlinedIcon } from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Card,
  Chip,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";

import type { HealthPage } from "../../../types/data-operations";
import {
  formatDataOperationsDateTime,
  healthStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";
import { CursorPager } from "./CursorPager";
import { DataOperationsTableEmptyState } from "./DataOperationsTableEmptyState";

/** 描述健康评估面板的数据和批次详情导航入口。 */
interface HealthEvaluationsPanelProps {
  data: HealthPage | undefined;
  isLoading: boolean;
  isError: boolean;
  canWrite: boolean;
  cursor?: string;
  onPageChange: (cursor: string | undefined) => void;
  onRefresh: () => void;
  onOpenEvaluation: (evaluationId: string) => void;
  onOpenHealthCheck: (healthCheckId: string) => void;
  onStartHealthCheck: () => void;
}

/** 展示不可变健康评估与当前开放问题数量，避免把两者混成一个历史事实。 */
export function HealthEvaluationsPanel({
  data,
  isLoading,
  isError,
  canWrite,
  cursor,
  onPageChange,
  onRefresh,
  onOpenEvaluation,
  onOpenHealthCheck,
  onStartHealthCheck,
}: HealthEvaluationsPanelProps) {
  return (
    <Stack spacing={2} component="section" aria-labelledby="health-evaluations-title">
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end">
        <Box>
          <Typography id="health-evaluations-title" variant="h4">
            健康度
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          {canWrite ? (
            <Button
              variant="contained"
              startIcon={<HealthAndSafetyOutlinedIcon />}
              onClick={onStartHealthCheck}
            >
              主动健康检查
            </Button>
          ) : null}
        </Stack>
      </Stack>
      {isError && data === undefined ? (
        <Alert
          severity="error"
          action={
            <Button color="inherit" size="small" onClick={onRefresh}>
              重试
            </Button>
          }
        >
          无法读取健康评估；读取失败不等于健康未知。
        </Alert>
      ) : null}
      <Card>
        <TableContainer sx={{ overflowX: "auto" }}>
          <Table size="small" aria-label="发布后健康评估">
            <TableHead>
              <TableRow>
                <TableCell>数据集</TableCell>
                <TableCell>状态 / 分数</TableCell>
                <TableCell>不可变评估</TableCell>
                <TableCell>当前问题投影</TableCell>
                <TableCell align="right">操作</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {/* 仅按服务端返回摘要显示，不在客户端合成规则结论。 */}
              {data !== undefined && data.items.length === 0 ? (
                <DataOperationsTableEmptyState
                  colSpan={5}
                  title="暂无健康评估"
                  description="执行健康检查后，评估结果会显示在这里。"
                />
              ) : null}
              {data?.items.map((evaluation) => (
                <TableRow key={evaluation.evaluationId} hover>
                  <TableCell>
                    <Typography fontWeight={700}>{evaluation.datasetCode}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      release {evaluation.releaseId}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      color={statusChipColor(evaluation.status)}
                      label={healthStatusLabel(evaluation.status)}
                    />
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ display: "block", mt: 0.5 }}
                    >
                      分数 {evaluation.score ?? "—"} · 警告 {evaluation.warningCount} · 严重{" "}
                      {evaluation.criticalCount}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">
                      策略 {evaluation.policyCode} v{evaluation.policyVersion}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      评估于 {formatDataOperationsDateTime(evaluation.evaluatedAt)}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">
                      开放问题 {evaluation.currentOpenIssueCount}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      投影于 {formatDataOperationsDateTime(evaluation.issueProjectionAsOf)}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    <Stack direction="row" justifyContent="flex-end" spacing={0.5}>
                      <Button
                        size="small"
                        onClick={() => onOpenEvaluation(evaluation.evaluationId)}
                      >
                        评估详情
                      </Button>
                      {evaluation.healthCheckId !== null ? (
                        <Button
                          size="small"
                          onClick={() => onOpenHealthCheck(evaluation.healthCheckId ?? "")}
                        >
                          检查批次
                        </Button>
                      ) : null}
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
        {data !== undefined ? (
          <CursorPager
            currentCursor={cursor}
            nextCursor={data.nextCursor}
            isLoading={isLoading}
            onPageChange={onPageChange}
          />
        ) : null}
      </Card>
    </Stack>
  );
}
