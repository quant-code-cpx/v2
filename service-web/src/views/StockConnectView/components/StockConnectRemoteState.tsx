import { Alert, Box, Button, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";

import { isApiError } from "../../../api/http";
import { stockConnectErrorCopy } from "../utils/stock-connect-presentation";
import type { StockConnectDateUrlValue } from "../utils/stock-connect-url";

/** 描述可恢复错误状态提供的操作。 */
interface StockConnectErrorStateProps {
  error: unknown;
  onRetry: () => void;
  onLatest?: () => void;
  /** 当前 URL 日期选择；榜单局部错误不提供日期恢复动作时可省略。 */
  dateSelection?: StockConnectDateUrlValue;
}

/** 在首次远程读取时保持接近最终四卡和主工作区的桌面几何。 */
export function StockConnectPageSkeleton() {
  return (
    <Stack spacing={3} aria-busy="true" aria-label="正在加载互联互通 publication">
      <Skeleton variant="rounded" height={50} />
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
          gap: 2,
        }}
      >
        {Array.from(
          { length: 4 },
          /** 为四条固定通道保留稳定骨架卡片。 */
          (_, index) => (
            <Skeleton key={index} variant="rounded" height={180} />
          ),
        )}
      </Box>
      <Skeleton variant="rounded" height={340} />
    </Stack>
  );
}

/** 显示无 publication、精确日缺失、权限或依赖故障，并保留恢复操作。 */
export function StockConnectErrorState({
  error,
  onRetry,
  onLatest,
  dateSelection,
}: StockConnectErrorStateProps) {
  const code = isApiError(error) ? error.code : undefined;
  const copy = stockConnectErrorCopy(code);
  const canReturnLatest =
    dateSelection !== undefined &&
    dateSelection !== "latest" &&
    (code === "EXACT_DATE_NOT_PUBLISHED" || code === "PUBLICATION_NOT_READY");

  return (
    <Card role="alert" aria-live="polite">
      <CardContent>
        <Stack spacing={2} alignItems="flex-start">
          <Alert severity={code === "AUTHORIZATION_FAILED" ? "error" : "warning"}>
            {copy.title}
          </Alert>
          <Typography color="text.secondary">{copy.description}</Typography>
          <Stack direction="row" spacing={1.5}>
            <Button variant="contained" onClick={onRetry}>
              重试
            </Button>
            {canReturnLatest && onLatest !== undefined ? (
              <Button variant="outlined" onClick={onLatest}>
                返回 latest
              </Button>
            ) : null}
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 描述一个不应被误解为全市场无数据的页内空状态。 */
interface StockConnectEmptyStateProps {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}

/** 显示来源榜或证券上下文的真实空结果与可选恢复操作。 */
export function StockConnectEmptyState({
  title,
  description,
  actionLabel,
  onAction,
}: StockConnectEmptyStateProps) {
  return (
    <Stack
      role="status"
      spacing={1.5}
      alignItems="center"
      justifyContent="center"
      sx={{ minHeight: 220, px: 3, py: 5, textAlign: "center" }}
    >
      <Typography variant="h6">{title}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 560 }}>
        {description}
      </Typography>
      {actionLabel !== undefined && onAction !== undefined ? (
        <Button variant="outlined" onClick={onAction}>
          {actionLabel}
        </Button>
      ) : null}
    </Stack>
  );
}
