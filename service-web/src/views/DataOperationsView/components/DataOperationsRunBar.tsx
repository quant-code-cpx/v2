import {
  ErrorOutline as ErrorOutlineIcon,
  SyncOutlined as SyncOutlinedIcon,
} from "@mui/icons-material";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Skeleton,
  Stack,
  Typography,
} from "@mui/material";

import type { OperationsOverview } from "../../../types/data-operations";
import { executionSlotLabel } from "../utils/data-operations-presentation";

/** 描述全局串行执行槽状态条所需的远程状态与刷新动作。 */
interface DataOperationsRunBarProps {
  overview: OperationsOverview | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
}

/** 展示唯一全局执行槽、排队数量及投递积压，绝不由页面推断并发状态。 */
export function DataOperationsRunBar({
  overview,
  isLoading,
  isError,
  onRetry,
}: DataOperationsRunBarProps) {
  if (isLoading && overview === undefined) {
    return <Skeleton variant="rounded" height={112} aria-label="正在加载全局串行执行状态" />;
  }

  if (isError && overview === undefined) {
    return (
      <Alert
        severity="error"
        action={
          <Button color="inherit" size="small" onClick={onRetry}>
            重试
          </Button>
        }
      >
        无法读取全局执行槽状态；不能据此判断队列空闲。
      </Alert>
    );
  }

  if (overview === undefined) {
    return null;
  }

  const { dataSync } = overview;
  const { executionSlot } = dataSync;

  return (
    <Card component="section" aria-labelledby="data-operations-run-bar-title">
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={3}>
          <Stack direction="row" spacing={2} alignItems="center">
            <Box
              aria-hidden="true"
              sx={{
                width: 40,
                height: 40,
                borderRadius: 1,
                display: "grid",
                placeItems: "center",
                bgcolor: executionSlot.state === "RUNNING" ? "primary.lighter" : "grey.100",
                color: executionSlot.state === "RUNNING" ? "primary.dark" : "text.secondary",
              }}
            >
              <SyncOutlinedIcon />
            </Box>
            <Box>
              <Typography id="data-operations-run-bar-title" variant="subtitle1">
                全局串行执行
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {executionSlot.state === "RUNNING" && executionSlot.datasetCode !== null
                  ? `当前运行：${executionSlot.datasetCode}`
                  : executionSlotLabel(executionSlot.state)}
              </Typography>
            </Box>
          </Stack>
          <Stack direction="row" spacing={3} alignItems="center">
            <Box>
              <Typography variant="overline" color="text.secondary">
                排队子任务
              </Typography>
              <Typography variant="h4">{dataSync.queuedRunCount}</Typography>
            </Box>
            <Box>
              <Typography variant="overline" color="text.secondary">
                待投递意图
              </Typography>
              <Typography variant="h4">{overview.deliveryPendingCount}</Typography>
            </Box>
            {overview.deliveryDeadLetterCount > 0 ? (
              <Chip
                icon={<ErrorOutlineIcon />}
                color="error"
                label={`待处理失败 ${overview.deliveryDeadLetterCount}`}
              />
            ) : null}
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}
