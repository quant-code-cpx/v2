import { Alert, Button, Card, CardContent, Skeleton, Stack, Typography } from "@mui/material";

import { isApiError } from "../../../api/http";
import type { EquityDatasetStatus } from "../../../types/equity-market";

/** 保留详情面板最终几何的加载状态。 */
export function DatasetLoading({ label }: { label: string }) {
  return (
    <Card aria-label={label}>
      <CardContent>
        <Stack spacing={1.5}>
          <Skeleton width="28%" height={30} />
          <Skeleton variant="rounded" height={280} />
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 渲染一个数据集没有合格 publication 或方法学的独立不可用状态。 */
export function DatasetUnavailable({
  title,
  status,
}: {
  title: string;
  status?: EquityDatasetStatus;
}) {
  const legitimateEmpty = status?.availability === "EMPTY";
  const partial = status?.availability === "PARTIAL";
  const sourceUnavailable = status?.availability === "SOURCE_UNAVAILABLE";
  const heading = legitimateEmpty
    ? `${title} publication 确认为空`
    : partial
      ? `${title}当前仅部分可用`
      : sourceUnavailable
        ? `${title}来源暂不可用`
        : `${title}尚无可用 publication`;

  return (
    <Alert severity={legitimateEmpty ? "info" : "warning"}>
      <Typography variant="subtitle2">{heading}</Typography>
      <Typography variant="body2" sx={{ mt: 0.5 }}>
        {status?.reasonCode ??
          (legitimateEmpty ? "LEGITIMATE_EMPTY" : partial ? "PARTIAL" : "NO_PUBLICATION")}{" "}
        · {status?.sourceLabel ?? "来源尚未发布"} · 页面不会用其他供应商或模型值静默填充。
      </Typography>
    </Alert>
  );
}

/** 渲染详情数据集的独立失败与可控重试。 */
export function DatasetError({
  title,
  error,
  retry,
}: {
  title: string;
  error: unknown;
  retry: () => void;
}) {
  const forbidden = isApiError(error) && error.status === 403;
  const limited = isApiError(error) && error.status === 429;

  return (
    <Alert
      severity={forbidden ? "warning" : "error"}
      action={
        forbidden ? undefined : (
          <Button color="inherit" size="small" onClick={retry} disabled={limited}>
            {limited ? "稍后重试" : "重试此数据集"}
          </Button>
        )
      }
    >
      {forbidden ? `当前账号无权读取${title}。` : `${title}读取失败，其他页签仍可继续使用。`}
    </Alert>
  );
}

/** 渲染 last-good 陈旧提醒，同时允许调用方继续显示只读内容。 */
export function DatasetStaleNotice({ status }: { status: EquityDatasetStatus }) {
  return (
    <Alert severity="info">
      新 publication 检查未通过，当前保留最后合格版本并只读展示：{status.publishedAt ?? "时间未知"}{" "}
      · {status.dataVersion ?? "版本未知"}
    </Alert>
  );
}
