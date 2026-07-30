import { lazy, Suspense } from "react";
import type { ComponentType, LazyExoticComponent } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Skeleton,
  Stack,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import { ArrowBackOutlined as ArrowBackOutlinedIcon } from "@mui/icons-material";
import { Link as RouterLink } from "react-router-dom";

import { isApiError } from "../../api/http";
import {
  exchangeLabel,
  formatDecimal,
  listingStatusLabel,
  marketDirection,
  tradingStatusLabel,
} from "../EquityMarketView/utils/equity-market-formatters";
import type { EquityDetailUrlState } from "../EquityMarketView/utils/equity-market-url";
import { useEquityDetail } from "./hooks/useEquityDetail";
import type { EquityDetailModel } from "./hooks/useEquityDetail";

/** 各页签独立拆包，避免首屏身份与行情为低优先级分析代码付费。 */
const tabPanels = {
  market: lazy(async () => {
    const { MarketTabPanel } = await import("./components/MarketTabPanel");
    return { default: MarketTabPanel };
  }),
  company: lazy(async () => {
    const { CompanyTabPanel } = await import("./components/CompanyTabPanel");
    return { default: CompanyTabPanel };
  }),
  financial: lazy(async () => {
    const { FinancialTabPanel } = await import("./components/FinancialTabPanel");
    return { default: FinancialTabPanel };
  }),
  valuation: lazy(async () => {
    const { ValuationTabPanel } = await import("./components/ValuationTabPanel");
    return { default: ValuationTabPanel };
  }),
  "money-flow": lazy(async () => {
    const { MoneyFlowTabPanel } = await import("./components/MoneyFlowTabPanel");
    return { default: MoneyFlowTabPanel };
  }),
  sectors: lazy(async () => {
    const { SectorsTabPanel } = await import("./components/SectorsTabPanel");
    return { default: SectorsTabPanel };
  }),
  events: lazy(async () => {
    const { EventsTabPanel } = await import("./components/EventsTabPanel");
    return { default: EventsTabPanel };
  }),
  "data-status": lazy(async () => {
    const { DataStatusTabPanel } = await import("./components/DataStatusTabPanel");
    return { default: DataStatusTabPanel };
  }),
} satisfies Record<
  EquityDetailUrlState["tab"],
  LazyExoticComponent<ComponentType<{ model: EquityDetailModel }>>
>;

/** 固定详情信息优先级：行情首屏，治理状态最后但始终可达。 */
const tabs: ReadonlyArray<{ value: EquityDetailUrlState["tab"]; label: string }> = [
  { value: "market", label: "行情与公司行动" },
  { value: "company", label: "公司概况" },
  { value: "financial", label: "财务" },
  { value: "valuation", label: "估值" },
  { value: "money-flow", label: "资金流" },
  { value: "sectors", label: "行业概念" },
  { value: "events", label: "证券事件" },
  { value: "data-status", label: "数据状态" },
];

/** 渲染无法安全解析的 canonical 证券路径。 */
function InvalidEquityPath() {
  return (
    <Alert
      severity="error"
      action={
        <Button component={RouterLink} to="/market/equities" color="inherit" size="small">
          返回列表
        </Button>
      }
    >
      证券路径无效；exchange 必须为 SSE、SZSE 或 BSE，symbol 必须为六位数字。
    </Alert>
  );
}

/** 渲染身份 publication 不可用、无权限、未找到和传输失败状态。 */
function IdentityError({ error, retry }: { error: unknown; retry: () => void }) {
  const apiError = isApiError(error) ? error : undefined;
  const noPublication = apiError?.status === 503 && apiError.code === "publication-unavailable";
  const notFound = apiError?.status === 404;
  const forbidden = apiError?.status === 403;
  const limited = apiError?.status === 429;
  return (
    <Card>
      <CardContent sx={{ minHeight: 320, display: "grid", placeItems: "center" }}>
        <Stack spacing={2} alignItems="center" sx={{ maxWidth: 620, textAlign: "center" }}>
          <Typography variant="h5">
            {noPublication
              ? "证券身份尚无 publication"
              : notFound
                ? "未找到该证券"
                : forbidden
                  ? "无权读取该证券"
                  : "证券身份读取失败"}
          </Typography>
          <Typography color="text.secondary">
            {noPublication
              ? "详情页不会绕过身份边界请求叶子数据，也不会根据股票代码猜测交易所。"
              : limited
                ? "请求频率已达到服务端限制，请按 Retry-After 稍后重试。"
                : "证券身份是详情的先决条件；失败时不会显示 fixture 或跨证券缓存。"}
          </Typography>
          <Stack direction="row" spacing={1}>
            <Button component={RouterLink} to="/market/equities" variant="outlined">
              返回股票中心
            </Button>
            {!forbidden && !notFound ? (
              <Button variant="contained" onClick={retry} disabled={limited}>
                {limited ? "稍后重试" : "重试身份"}
              </Button>
            ) : null}
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 渲染 canonical 个股详情和独立数据集页签。 */
export function EquityDetailView() {
  const model = useEquityDetail();
  if (!model.validIdentity) return <InvalidEquityPath />;

  if (model.identityQuery.isPending) {
    return (
      <Stack spacing={2}>
        <Skeleton variant="rounded" height={170} aria-label="正在加载证券身份" />
        <Skeleton variant="rounded" height={54} />
        <Skeleton variant="rounded" height={520} />
      </Stack>
    );
  }

  if (model.identityQuery.isError || model.identity === undefined) {
    return (
      <IdentityError
        error={model.identityQuery.error}
        retry={() => void model.identityQuery.refetch()}
      />
    );
  }

  const identity = model.identity;
  const quote = model.discoveryRecord;
  const direction = marketDirection(quote?.market.changePercent);
  const ActivePanel = tabPanels[model.state.tab];

  /** 同时复验身份、详情状态和精确 discovery 行，叶子数据由各页签独立重试。 */
  function refreshHeader(): void {
    void Promise.all([
      model.identityQuery.refetch(),
      model.statusQuery.refetch(),
      model.discoveryQuery.refetch(),
    ]);
  }

  return (
    <Stack spacing={2}>
      <Button
        component={RouterLink}
        to="/market/equities"
        startIcon={<ArrowBackOutlinedIcon />}
        sx={{ alignSelf: "flex-start" }}
      >
        返回股票中心
      </Button>

      <Card>
        <CardContent>
          <Stack direction="row" justifyContent="space-between" spacing={3}>
            <Box sx={{ minWidth: 0 }}>
              <Stack direction="row" alignItems="center" spacing={1.25}>
                <Typography variant="h4" noWrap>
                  {identity.name.value}
                </Typography>
                <Chip
                  size="small"
                  label={`${exchangeLabel(identity.identifier.exchange)} · ${identity.identifier.symbol}`}
                />
                <Chip
                  size="small"
                  color={identity.listing.status === "LISTED" ? "success" : "warning"}
                  variant="outlined"
                  label={listingStatusLabel(identity.listing.status)}
                />
              </Stack>
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                身份有效期 {identity.identifier.effectiveFrom} 至{" "}
                {identity.identifier.effectiveTo ?? "当前"} · knownFrom{" "}
                {identity.identifier.knownFrom}
              </Typography>
              <Stack
                direction="row"
                divider={<Divider orientation="vertical" flexItem />}
                spacing={2}
                sx={{ mt: 2 }}
              >
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    最近 EOD 收盘
                  </Typography>
                  <Typography variant="h5">
                    {formatDecimal(quote?.market.close)}
                    <Typography component="span" variant="caption" color="text.secondary">
                      {" "}
                      CNY
                    </Typography>
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    涨跌幅
                  </Typography>
                  <Typography variant="h6" sx={{ color: direction.color }}>
                    {direction.label}
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    目标日交易状态
                  </Typography>
                  <Typography variant="subtitle1">
                    {quote === undefined
                      ? "尚无已发布值"
                      : tradingStatusLabel(quote.statuses.tradingStatus)}
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    行情日期
                  </Typography>
                  <Typography variant="subtitle1">{quote?.market.tradeDate ?? "—"}</Typography>
                </Box>
              </Stack>
            </Box>
            <Stack alignItems="flex-end" spacing={1} sx={{ flexShrink: 0 }}>
              <Chip
                color="success"
                variant="outlined"
                label={`身份发布 ${identity.effectiveAsOf}`}
              />
              <Typography variant="caption" color="text.secondary">
                {identity.publishedAt}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ maxWidth: 300 }}>
                dataVersion {identity.dataVersion}
              </Typography>
              <Button
                variant="outlined"
                onClick={refreshHeader}
                disabled={
                  model.identityQuery.isFetching ||
                  model.statusQuery.isFetching ||
                  model.discoveryQuery.isFetching
                }
              >
                刷新已发布数据
              </Button>
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      {model.discoveryQuery.isError ? (
        <Alert severity="warning">
          身份已加载，但 discovery 报价读取失败；公司、财务和事件等独立数据仍可继续访问。
        </Alert>
      ) : null}
      {model.discovery?.availability === "UNAVAILABLE" ? (
        <Alert severity="warning">
          股票横截面尚无 publication，头部报价保持“尚无已发布值”；详情不会填充实时或估算行情。
        </Alert>
      ) : null}
      {model.statusQuery.isError ? (
        <Alert
          severity="warning"
          action={
            <Button color="inherit" size="small" onClick={() => void model.statusQuery.refetch()}>
              重试状态
            </Button>
          }
        >
          数据状态读取失败；不依赖方法学发现的页签仍可使用，受影响页签会保持独立失败状态。
        </Alert>
      ) : null}

      <Card>
        <Tabs
          value={model.state.tab}
          onChange={(_event, value: EquityDetailUrlState["tab"]) =>
            model.updateState({ tab: value })
          }
          variant="scrollable"
          scrollButtons={false}
          aria-label="个股详情页签"
          sx={{ px: 1 }}
        >
          {/* 页签顺序体现行情、公司基本面、分析、事件、治理元数据的信息优先级。 */}
          {tabs.map((tab) => (
            <Tab key={tab.value} value={tab.value} label={tab.label} />
          ))}
        </Tabs>
      </Card>

      <Suspense fallback={<Skeleton variant="rounded" height={520} />}>
        <ActivePanel model={model} />
      </Suspense>
    </Stack>
  );
}
