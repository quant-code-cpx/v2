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
import { CloudOffOutlined as CloudOffOutlinedIcon } from "@mui/icons-material";

import { isApiError } from "../../api/http";
import { EquityMarketFilters } from "./components/EquityMarketFilters";
import { EquityMarketTable } from "./components/EquityMarketTable";
import { useEquityMarket } from "./hooks/useEquityMarket";

/** 渲染接近最终表格几何的首屏加载骨架。 */
function EquityMarketLoading() {
  return (
    <Card aria-label="正在加载股票列表">
      <CardContent>
        <Stack spacing={1}>
          <Skeleton variant="rounded" height={56} />
          {Array.from({ length: 8 }, (_, index) => (
            <Skeleton key={index} variant="rounded" height={52} />
          ))}
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 将公开错误映射为不会泄漏内部依赖细节的恢复界面。 */
function EquityMarketError({ error, retry }: { error: unknown; retry: () => void }) {
  const forbidden = isApiError(error) && error.status === 403;
  const limited = isApiError(error) && error.status === 429;
  const retryAfter = isApiError(error) ? error.retryAfterSeconds : undefined;

  return (
    <Alert
      severity={forbidden ? "warning" : "error"}
      action={
        forbidden ? undefined : (
          <Button color="inherit" size="small" onClick={retry} disabled={limited}>
            {limited ? "稍后重试" : "重试"}
          </Button>
        )
      }
    >
      {forbidden
        ? "当前账号无权读取股票中心。"
        : limited
          ? `请求过于频繁，请在 ${retryAfter ?? "服务端指定"} 秒后重试。`
          : "股票数据暂时无法读取；若已有缓存，页面不会以空结果覆盖它。"}
    </Alert>
  );
}

/** 渲染与合法零条结果明确不同的无 publication 阻断状态。 */
function EquityPublicationUnavailable({
  reasonCode,
  retry,
}: {
  reasonCode: string | null | undefined;
  retry: () => void;
}) {
  return (
    <Card>
      <CardContent sx={{ minHeight: 320, display: "grid", placeItems: "center" }}>
        <Stack spacing={2} alignItems="center" sx={{ maxWidth: 560, textAlign: "center" }}>
          <CloudOffOutlinedIcon color="error" sx={{ fontSize: 52 }} />
          <Typography variant="h5">股票目录尚无可用发布版本</Typography>
          <Typography color="text.secondary">
            页面不会回退到 fixture、可变表“最新行”或手填行情。请等待数据运维完成合格的基础
            publication 后重新检查。
          </Typography>
          <Chip
            label={`原因：${reasonCode ?? "NO_PUBLICATION"}`}
            color="error"
            variant="outlined"
          />
          <Button variant="contained" onClick={retry}>
            重新检查
          </Button>
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 渲染统一 A 股证券发现页面。 */
export function EquityMarketView() {
  const model = useEquityMarket();
  const response = model.response;
  const unavailableComponents =
    response?.components.filter((component) => component.availability !== "AVAILABLE") ?? [];

  /** 手动重试只复验当前列表，不调用数据同步控制面。 */
  const retry = () => {
    void model.searchQuery.refetch();
  };

  return (
    <Stack spacing={2.5}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Typography variant="h4">股票中心</Typography>
          <Typography color="text.secondary" sx={{ mt: 0.5 }}>
            统一发现沪、深、北交所 A 股证券；全部行情均为最近合格 EOD publication，非实时。
          </Typography>
        </Box>
        {response?.release === null || response?.release === undefined ? (
          <Chip label="尚无 publication" color="warning" variant="outlined" />
        ) : (
          <Stack alignItems="flex-end" spacing={0.5}>
            <Chip
              label={`${response.release.completeness === "FULL" ? "完整发布" : "部分发布"} · ${
                response.release.effectiveAsOf ?? "日期未知"
              }`}
              color={response.release.completeness === "FULL" ? "success" : "warning"}
            />
            <Typography variant="caption" color="text.secondary">
              {response.release.publishedAt} · {response.release.dataVersion}
            </Typography>
          </Stack>
        )}
      </Stack>

      <EquityMarketFilters model={model} />

      {model.searchQuery.isPending ? <EquityMarketLoading /> : null}
      {model.searchQuery.isError ? (
        <EquityMarketError error={model.searchQuery.error} retry={retry} />
      ) : null}
      {response?.availability === "UNAVAILABLE" ? (
        <EquityPublicationUnavailable reasonCode={response.reasonCode} retry={retry} />
      ) : null}
      {response?.availability === "AVAILABLE" && response.release?.completeness === "PARTIAL" ? (
        <Alert severity="warning">
          当前 publication 明确标记为 PARTIAL
          {unavailableComponents.length > 0
            ? `，包含 ${unavailableComponents.length} 个不可用或部分组件`
            : ""}
          ；缺值均保留原因，不以零或其他供应商静默补齐。
        </Alert>
      ) : null}
      {model.searchQuery.isPlaceholderData ? (
        <Alert severity="info">
          新筛选正在读取；当前表格仅保留上一页几何，完成前不可视为匹配结果。
        </Alert>
      ) : null}
      {response?.availability === "AVAILABLE" && response.records.length === 0 ? (
        <Card>
          <CardContent sx={{ minHeight: 220, display: "grid", placeItems: "center" }}>
            <Stack spacing={1.5} alignItems="center">
              <Typography variant="h6">当前筛选没有证券</Typography>
              <Typography color="text.secondary">
                publication 已存在，因此这是合法空结果，不是数据源故障。
              </Typography>
              <Button variant="outlined" onClick={model.reset}>
                清除筛选
              </Button>
            </Stack>
          </CardContent>
        </Card>
      ) : null}
      {response?.availability === "AVAILABLE" && response.records.length > 0 ? (
        <EquityMarketTable model={model} />
      ) : null}
    </Stack>
  );
}
