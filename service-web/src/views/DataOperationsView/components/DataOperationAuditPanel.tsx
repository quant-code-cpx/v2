import { ContentCopyOutlined as ContentCopyOutlinedIcon } from "@mui/icons-material";
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
import { useCallback } from "react";

import type { OperationPage } from "../../../types/data-operations";
import {
  actorDisplayLabel,
  deliveryStatusLabel,
  errorSummaryLabel,
  formatDataOperationsDateTime,
  operationResultLabel,
  statusChipColor,
} from "../utils/data-operations-presentation";
import { CursorPager } from "./CursorPager";
import { DataOperationsTableEmptyState } from "./DataOperationsTableEmptyState";

/** 描述公开操作记录面板的远程数据和刷新入口。 */
interface DataOperationAuditPanelProps {
  data: OperationPage | undefined;
  isLoading: boolean;
  isError: boolean;
  cursor?: string;
  onPageChange: (cursor: string | undefined) => void;
  onRefresh: () => void;
}

/** 仅复制稳定错误码和 requestId，避免复制安全摘要之外的服务端正文。 */
function copyOperationReference(code: string | undefined, requestId: string): void {
  void navigator.clipboard?.writeText(
    [code, requestId].filter((value) => value !== undefined).join("\n"),
  );
}

/** 展示 USER/SYSTEM 操作投影、投递状态与动作结论，不暴露内部主体引用。 */
export function DataOperationAuditPanel({
  data,
  isLoading,
  isError,
  cursor,
  onPageChange,
  onRefresh,
}: DataOperationAuditPanelProps) {
  /** 将一条安全关联标识复制到剪贴板。 */
  const handleCopyReference = useCallback((code: string | undefined, requestId: string) => {
    copyOperationReference(code, requestId);
  }, []);

  return (
    <Stack spacing={2} component="section" aria-labelledby="operation-audit-title">
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end">
        <Box>
          <Typography id="operation-audit-title" variant="h4">
            操作记录
          </Typography>
        </Box>
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
          无法读取操作记录。
        </Alert>
      ) : null}
      <Card>
        <TableContainer sx={{ overflowX: "auto" }}>
          <Table size="small" aria-label="数据运维操作记录">
            <TableHead>
              <TableRow>
                <TableCell>时间 / 操作人</TableCell>
                <TableCell>动作 / 目标</TableCell>
                <TableCell>投递 / 动作结果</TableCell>
                <TableCell>原因 / 错误</TableCell>
                <TableCell align="right">关联</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {/* 操作者只使用 ActorDisplay，SYSTEM 绝不能显示为已删除用户。 */}
              {data !== undefined && data.items.length === 0 ? (
                <DataOperationsTableEmptyState
                  colSpan={5}
                  title="暂无操作记录"
                  description="提交、取消或重试任务后的记录会显示在这里。"
                />
              ) : null}
              {data?.items.map((operation) => (
                <TableRow
                  key={`${operation.submissionId ?? "system"}-${operation.requestId}`}
                  hover
                >
                  <TableCell>
                    <Typography variant="body2">
                      {formatDataOperationsDateTime(operation.authorizedAt)}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {actorDisplayLabel(operation.actor)} · {operation.actor.actorType}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography fontWeight={700}>{operation.action}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {operation.targetSummary}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      color={statusChipColor(
                        operation.deliveryStatus === "NOT_APPLICABLE"
                          ? "ACCEPTED"
                          : operation.deliveryStatus,
                      )}
                      label={deliveryStatusLabel(
                        operation.deliveryStatus === "NOT_APPLICABLE"
                          ? "ACCEPTED"
                          : operation.deliveryStatus,
                      )}
                    />
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ display: "block", mt: 0.5 }}
                    >
                      {operation.deliveryStatus === "NOT_APPLICABLE"
                        ? "系统来源，不适用"
                        : operationResultLabel(operation.operationResult)}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">{operation.reason}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {errorSummaryLabel(operation.error)}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    <Typography
                      variant="caption"
                      sx={{ display: "block", fontFamily: "monospace" }}
                    >
                      {operation.requestId}
                    </Typography>
                    <Button
                      size="small"
                      startIcon={<ContentCopyOutlinedIcon />}
                      onClick={() =>
                        handleCopyReference(operation.error?.code, operation.requestId)
                      }
                    >
                      复制 code / requestId
                    </Button>
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
