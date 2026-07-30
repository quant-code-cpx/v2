import { Alert, Button, Card, CardContent, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

import { MarketDataPublication } from "../../../components/MarketDataPublication";
import { MarketDataStateView } from "../../../components/MarketDataStateView";
import type { MarketDataPageMeta } from "../../../types/etf";
import { unavailableReleaseSummary } from "../../../utils/etf-presentation";

/** ETF 详情数据集区块的互斥远程状态。 */
export type EtfDatasetSectionState =
  | "loading"
  | "error"
  | "source-unavailable"
  | "currently-unsupported"
  | "empty"
  | "available";

/** ETF 数据集区块需要的标题、状态、publication 与内容。 */
interface EtfDatasetSectionProps {
  title: string;
  description: string;
  datasetLabel: string;
  state: EtfDatasetSectionState;
  meta?: MarketDataPageMeta;
  refreshFailed?: boolean;
  onRetry: () => void;
  children?: ReactNode;
}

/** 让日线、NAV 和状态各自拥有独立加载、失败、空结果与 publication 边界。 */
export function EtfDatasetSection({
  title,
  description,
  datasetLabel,
  state,
  meta,
  refreshFailed = false,
  onRetry,
  children,
}: EtfDatasetSectionProps) {
  return (
    <Card component="section">
      <CardContent sx={{ p: 3 }}>
        <Stack spacing={2}>
          <div>
            <Typography component="h2" variant="h6">
              {title}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {description}
            </Typography>
          </div>
          {refreshFailed && meta !== undefined ? (
            <Alert
              severity="warning"
              action={
                <Button color="inherit" size="small" onClick={onRetry}>
                  重试
                </Button>
              }
            >
              {title}刷新失败，仍展示上一份已校验 publication。
            </Alert>
          ) : null}
          {state === "loading" ? (
            <MarketDataStateView
              kind="loading"
              title={`正在读取${title}`}
              description="等待独立 typed dataset 返回。"
            />
          ) : null}
          {state === "error" ? (
            <MarketDataStateView
              kind="error"
              title={`${title}请求失败`}
              description="该区块可独立重试，其他已成功数据不会被清空。"
              onRetry={onRetry}
            />
          ) : null}
          {state === "source-unavailable" ? (
            <MarketDataStateView
              kind="source-unavailable"
              title={`${title}暂无可读 publication`}
              description={`${meta === undefined ? "PUBLICATION_NOT_AVAILABLE" : unavailableReleaseSummary(meta)}。不会使用本地假数据补齐。`}
              onRetry={onRetry}
            />
          ) : null}
          {state === "currently-unsupported" ? (
            <MarketDataStateView
              kind="currently-unsupported"
              title={`${title}当前不支持`}
              description={`${meta === undefined ? "CURRENTLY_UNSUPPORTED" : unavailableReleaseSummary(meta)}。来源口径未能安全映射到冻结合同，不会伪装成单位 NAV 或其他数值。`}
            />
          ) : null}
          {state === "empty" ? (
            <MarketDataStateView
              kind="empty"
              title={`${title}当前窗口无记录`}
              description={`${meta === undefined ? "EMPTY" : unavailableReleaseSummary(meta)}。当前没有可公开记录；不从其他数据集推导或填补。`}
            />
          ) : null}
          {state === "available" && meta !== undefined ? (
            <>
              <MarketDataPublication datasetLabel={datasetLabel} meta={meta} />
              {children}
            </>
          ) : null}
        </Stack>
      </CardContent>
    </Card>
  );
}
