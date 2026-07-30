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
import { useCallback, useState } from "react";

import { dataHealthDetailQueryOptions } from "../../../api/data-operations";
import {
  formatDataOperationsDateTime,
  healthStatusLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";

/** 描述健康评估详情 Drawer 的独立问题 cursor。 */
interface HealthEvaluationDetailDrawerProps {
  evaluationId: string | undefined;
  onClose: () => void;
}

/** 展示 immutable evaluation 事实及独立分页的 current open issue 投影。 */
export function HealthEvaluationDetailDrawer({
  evaluationId,
  onClose,
}: HealthEvaluationDetailDrawerProps) {
  const [issuesCursor, setIssuesCursor] = useState<string | null>(null);
  const evaluationQuery = useQuery({
    ...dataHealthDetailQueryOptions({
      evaluationId: evaluationId ?? "",
      issuesCursor,
      issuesLimit: 100,
    }),
    enabled: evaluationId !== undefined,
  });

  /** 关闭评估详情并清理 URL。 */
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  /** 只推进当前开放问题 cursor，不修改 immutable evaluation。 */
  const handleLoadMoreIssues = useCallback(() => {
    if (
      evaluationQuery.data?.currentOpenIssuesNextCursor !== null &&
      evaluationQuery.data?.currentOpenIssuesNextCursor !== undefined
    ) {
      setIssuesCursor(evaluationQuery.data.currentOpenIssuesNextCursor);
    }
  }, [evaluationQuery.data?.currentOpenIssuesNextCursor]);

  return (
    <Drawer
      anchor="right"
      open={evaluationId !== undefined}
      onClose={handleClose}
      aria-labelledby="health-evaluation-detail-title"
    >
      <Box sx={{ p: 3, display: "flex", flexDirection: "column", gap: 2 }}>
        {evaluationQuery.isPending ? (
          <Skeleton variant="rounded" height={540} aria-label="正在加载健康评估详情" />
        ) : null}
        {evaluationQuery.isError ? <Alert severity="error">无法读取健康评估详情。</Alert> : null}
        {evaluationQuery.data !== undefined ? (
          <>
            <Typography id="health-evaluation-detail-title" variant="h5">
              健康评估详情
            </Typography>
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
              {evaluationQuery.data.evaluation.evaluationId}
            </Typography>
            <Chip
              color={statusChipColor(evaluationQuery.data.evaluation.status)}
              label={healthStatusLabel(evaluationQuery.data.evaluation.status)}
            />
            <Typography variant="body2" color="text.secondary">
              dataVersion {evaluationQuery.data.evaluation.dataVersion} · release{" "}
              {evaluationQuery.data.evaluation.releaseId}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              策略 {evaluationQuery.data.evaluation.policyCode} v
              {evaluationQuery.data.evaluation.policyVersion} · 评估于{" "}
              {formatDataOperationsDateTime(evaluationQuery.data.evaluation.evaluatedAt)}
            </Typography>
            <Divider />
            <Box component="section" aria-labelledby="health-rule-results-title">
              <Typography id="health-rule-results-title" variant="subtitle1">
                不可变规则结果
              </Typography>
              {/* 规则结果是历史事实；问题 ACK 状态不会改写本区内容。 */}
              {evaluationQuery.data.evaluation.results.map((result) => (
                <Stack
                  key={`${result.ruleCode}-${result.dimension}`}
                  spacing={0.25}
                  sx={{ mt: 1.5 }}
                >
                  <Typography variant="body2">
                    {result.ruleCode} · {result.dimension} · {result.severity} / {result.status}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    预期 {result.expected ?? "—"} · 观测 {result.observed ?? "—"} · 影响{" "}
                    {result.affectedCount ?? "—"} · {result.message}
                  </Typography>
                </Stack>
              ))}
            </Box>
            <Divider />
            <Box component="section" aria-labelledby="health-current-issues-title">
              <Typography id="health-current-issues-title" variant="subtitle1">
                当前开放问题（{evaluationQuery.data.currentOpenIssueCount}）
              </Typography>
              <Typography variant="caption" color="text.secondary">
                投影于 {formatDataOperationsDateTime(evaluationQuery.data.issueProjectionAsOf)}
              </Typography>
              {/* 仅显示 OPEN / ACKNOWLEDGED 当前投影，脱敏证据由服务端限界。 */}
              {evaluationQuery.data.currentOpenIssues.map((issue) => (
                <Stack key={issue.issueId} spacing={0.25} sx={{ mt: 1.5 }}>
                  <Typography variant="body2">
                    {issue.ruleCode} · {issue.dimension} · {issue.severity} / {issue.status}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    影响 {issue.affectedCount ?? "—"} · {issue.evidenceSummary ?? "无公开证据摘要"}
                  </Typography>
                </Stack>
              ))}
              {evaluationQuery.data.currentOpenIssuesNextCursor !== null ? (
                <Button size="small" onClick={handleLoadMoreIssues}>
                  加载更多问题
                </Button>
              ) : null}
            </Box>
          </>
        ) : null}
      </Box>
    </Drawer>
  );
}
