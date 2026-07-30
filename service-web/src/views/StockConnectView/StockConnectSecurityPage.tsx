import {
  Card,
  CardContent,
  Chip,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Typography,
} from "@mui/material";
import { useCallback } from "react";
import { useParams } from "react-router-dom";
import type { SelectChangeEvent } from "@mui/material";

import {
  StockConnectDateFilter,
  StockConnectTrendDaysSelect,
} from "./components/StockConnectFilters";
import { StockConnectPageHeader } from "./components/StockConnectPageHeader";
import {
  StockConnectErrorState,
  StockConnectPageSkeleton,
} from "./components/StockConnectRemoteState";
import { PublicationBar } from "./components/PublicationBar";
import { SecurityActivityTable } from "./components/SecurityActivityTable";
import { useStockConnectSecurityQuery } from "./hooks/useStockConnectQueries";
import { useStockConnectSecurityUrlState } from "./hooks/useStockConnectUrlState";
import {
  stockConnectChannelDescription,
  stockConnectChannelLabel,
} from "./utils/stock-connect-presentation";
import { stockConnectChannelCodeBySlug, stockConnectChannelSlugs } from "./utils/stock-connect-url";
import type {
  StockConnectChannelSlug,
  StockConnectDateUrlValue,
  StockConnectTrendDays,
} from "./utils/stock-connect-url";

/** 从已通过 loader 的参数读取稳定证券引用，异常时拒绝发起 API。 */
function resolveInstrumentEntityRef(value: string | undefined): string {
  if (value === undefined || value.length === 0 || value.length > 160) {
    throw new Response("Not Found", { status: 404 });
  }

  return value;
}

/** 渲染证券在互联互通来源活跃榜内的稳定身份与历史上下文。 */
export function StockConnectSecurityPage() {
  const parameters = useParams<{ instrumentEntityRef: string }>();
  const instrumentEntityRef = resolveInstrumentEntityRef(parameters.instrumentEntityRef);
  const { state, update } = useStockConnectSecurityUrlState();
  const query = useStockConnectSecurityQuery(instrumentEntityRef, state);
  const response = query.data?.data;

  /** 更新精确交易日或 latest。 */
  const handleDateChange = useCallback(
    (date: StockConnectDateUrlValue) => {
      update({ date });
    },
    [update],
  );

  /** 更新可选通道；all 映射合同 null 而不是伪造聚合通道。 */
  const handleChannelChange = useCallback(
    (event: SelectChangeEvent<string>) => {
      const value = event.target.value;
      update({
        channel: value === "all" ? undefined : (value as StockConnectChannelSlug),
      });
    },
    [update],
  );

  /** 更新历史交易日窗口。 */
  const handleTrendDaysChange = useCallback(
    (trendDays: StockConnectTrendDays) => {
      update({ trendDays });
    },
    [update],
  );

  /** 重试证券上下文 publication 查询。 */
  const handleRetry = useCallback(() => {
    void query.refetch();
  }, [query]);

  /** 从精确日缺失状态恢复到 latest。 */
  const handleReturnLatest = useCallback(() => {
    update({ date: "latest" });
  }, [update]);

  return (
    <Stack spacing={3}>
      <StockConnectPageHeader
        eyebrow="市场数据 / 互联互通 / 证券上下文"
        title={response?.identity.displayName ?? "证券互联互通上下文"}
        description="只展示该证券在互联互通通道和官方来源活跃榜内的表现，不扩展为完整港股行情、持仓、订单或结算。"
        breadcrumb={
          response?.identity.displayName ?? response?.identity.sourceSecurityCode ?? "证券上下文"
        }
        actions={
          response === undefined ? undefined : (
            <Stack direction="row" spacing={1}>
              <Chip
                color={
                  response.identity.identityAvailability === "RESOLVED" ? "success" : "warning"
                }
                label={
                  response.identity.identityAvailability === "RESOLVED"
                    ? "稳定身份"
                    : "来源身份未解析"
                }
              />
              <Chip label={response.identity.listingVenue} />
            </Stack>
          )
        }
      />
      <Stack direction="row" spacing={1.5}>
        <StockConnectDateFilter value={state.date} onChange={handleDateChange} />
        <FormControl sx={{ width: 220 }}>
          <InputLabel id="stock-connect-security-channel-label">通道</InputLabel>
          <Select
            labelId="stock-connect-security-channel-label"
            label="通道"
            value={state.channel ?? "all"}
            onChange={handleChannelChange}
          >
            <MenuItem value="all">全部通道（逐行原币）</MenuItem>
            {stockConnectChannelSlugs.map(
              /** 渲染四条稳定通道，不提供跨币种汇总选项。 */
              (slug) => {
                const channel = stockConnectChannelCodeBySlug[slug];
                return (
                  <MenuItem key={slug} value={slug}>
                    {stockConnectChannelLabel(channel)} · {stockConnectChannelDescription(channel)}
                  </MenuItem>
                );
              },
            )}
          </Select>
        </FormControl>
        <StockConnectTrendDaysSelect value={state.trendDays} onChange={handleTrendDaysChange} />
      </Stack>

      {response === undefined && query.isPending ? (
        <StockConnectPageSkeleton />
      ) : response === undefined && query.isError ? (
        <StockConnectErrorState
          error={query.error}
          onRetry={handleRetry}
          onLatest={handleReturnLatest}
          dateSelection={state.date}
        />
      ) : response !== undefined ? (
        <>
          <PublicationBar
            publication={response.publication}
            resolvedTradeDate={response.resolvedTradeDate}
            resolutionLabel="上下文交易日"
            isFetching={query.isFetching}
            isStaleBecauseError={query.isError}
          />
          <Card>
            <CardContent>
              <Stack spacing={1.5}>
                <Typography variant="h5">
                  {response.identity.displayName ?? response.identity.sourceSecurityCode}
                </Typography>
                <Typography color="text.secondary">
                  {response.identity.sourceSecurityCode} · {response.identity.listingVenue}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  instrumentEntityRef：{response.identity.instrumentEntityRef ?? "— 来源身份未解析"}
                </Typography>
              </Stack>
            </CardContent>
          </Card>
          <Card>
            <CardContent>
              <Stack spacing={2}>
                <Stack>
                  <Typography variant="h5">互联互通来源榜活动</Typography>
                  <Typography variant="body2" color="text.secondary">
                    共 {response.activities.length}{" "}
                    条来源榜记录；每行保留通道、日期和原币，不跨币种求和。
                  </Typography>
                </Stack>
                <SecurityActivityTable activities={response.activities} />
              </Stack>
            </CardContent>
          </Card>
        </>
      ) : null}
    </Stack>
  );
}
