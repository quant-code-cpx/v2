import { AddOutlined as AddOutlinedIcon } from "@mui/icons-material";
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

import type { SchedulePage, ScheduleView } from "../../../types/data-operations";
import {
  actorDisplayLabel,
  formatDataOperationsDateTime,
} from "../utils/data-operations-presentation";
import { targetSelectorSummary } from "../utils/target-selector";
import { CursorPager } from "./CursorPager";
import { DataOperationsTableEmptyState } from "./DataOperationsTableEmptyState";

/** 描述自动计划面板的远程数据及编辑、启停入口。 */
interface SchedulePanelProps {
  data: SchedulePage | undefined;
  isLoading: boolean;
  isError: boolean;
  canWrite: boolean;
  cursor?: string;
  onPageChange: (cursor: string | undefined) => void;
  onRefresh: () => void;
  onOpenEditor: (scheduleId?: string) => void;
  onSetEnabled: (schedule: ScheduleView) => void;
}

/** 展示计划频率、策略、下一次运行和乐观锁版本，并提供超级管理员操作入口。 */
export function SchedulePanel({
  data,
  isLoading,
  isError,
  canWrite,
  cursor,
  onPageChange,
  onRefresh,
  onOpenEditor,
  onSetEnabled,
}: SchedulePanelProps) {
  return (
    <Stack spacing={2} component="section" aria-labelledby="schedule-panel-title">
      <Stack direction="row" justifyContent="space-between" alignItems="flex-end">
        <Box>
          <Typography id="schedule-panel-title" variant="h4">
            自动计划
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          {canWrite ? (
            <Button
              variant="contained"
              startIcon={<AddOutlinedIcon />}
              onClick={() => onOpenEditor()}
            >
              新建计划
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
          无法读取自动计划；不能显示为没有计划。
        </Alert>
      ) : null}
      <Card>
        <TableContainer sx={{ overflowX: "auto" }}>
          <Table size="small" aria-label="自动同步计划">
            <TableHead>
              <TableRow>
                <TableCell>数据集</TableCell>
                <TableCell>频率 / 时区</TableCell>
                <TableCell>同步策略</TableCell>
                <TableCell>下次运行</TableCell>
                <TableCell>状态 / 版本</TableCell>
                <TableCell align="right">操作</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {/* 计划内容完全使用服务端当前 revision，编辑时不允许改绑数据集。 */}
              {data !== undefined && data.items.length === 0 ? (
                <DataOperationsTableEmptyState
                  colSpan={6}
                  title="暂无自动计划"
                  description="新建计划后，会在这里显示下一次运行时间和状态。"
                />
              ) : null}
              {data?.items.map((schedule) => (
                <TableRow key={schedule.scheduleId} hover>
                  <TableCell>
                    <Typography fontWeight={700}>{schedule.datasetCode}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      更新人 {actorDisplayLabel(schedule.updatedBy)}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">
                      {schedule.frequency.kind}{" "}
                      {schedule.frequency.localTime ??
                        `每 ${schedule.frequency.intervalMinutes ?? "—"} 分钟`}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {schedule.frequency.timezone}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2">{schedule.mode}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {targetSelectorSummary(schedule.selector)} ·{" "}
                      {schedule.targetPolicy.dateResolution} · policy v
                      {schedule.targetPolicy.policyVersion}
                    </Typography>
                  </TableCell>
                  <TableCell>{formatDataOperationsDateTime(schedule.nextRunAt)}</TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      color={schedule.enabled ? "success" : "default"}
                      label={schedule.enabled ? "已启用" : "已暂停"}
                    />
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ display: "block", mt: 0.5 }}
                    >
                      版本 {schedule.version}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    {canWrite ? (
                      <Stack direction="row" justifyContent="flex-end" spacing={0.5}>
                        <Button size="small" onClick={() => onOpenEditor(schedule.scheduleId)}>
                          编辑
                        </Button>
                        <Button size="small" onClick={() => onSetEnabled(schedule)}>
                          {schedule.enabled ? "暂停" : "启用"}
                        </Button>
                      </Stack>
                    ) : (
                      <Typography variant="caption" color="text.secondary">
                        仅查看
                      </Typography>
                    )}
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
