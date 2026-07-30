import {
  Box,
  Button,
  Chip,
  Link,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TableSortLabel,
  Tooltip,
  Typography,
} from "@mui/material";
import { Link as RouterLink } from "react-router-dom";

import type { EquitySearchRecord, EquitySearchSortField } from "../../../types/equity-market";
import type { EquityMarketModel } from "../hooks/useEquityMarket";
import {
  exchangeLabel,
  formatCny,
  formatDecimal,
  listingStatusLabel,
  marketDirection,
  nullReasonLabel,
  tradingStatusLabel,
} from "../utils/equity-market-formatters";

/** 描述一个能触发服务端排序的表头。 */
interface SortableHeadProps {
  field: EquitySearchSortField;
  label: string;
  model: EquityMarketModel;
  align?: "left" | "right";
}

/** 渲染带明确升降序文本的可键盘操作表头。 */
function SortableHead({ field, label, model, align = "right" }: SortableHeadProps) {
  const active = model.state.sort === field;

  /** 请求服务端切换当前列排序。 */
  const handleSort = () => model.setSort(field);

  return (
    <TableCell align={align} sortDirection={active ? model.state.order : false}>
      <TableSortLabel
        active={active}
        direction={active ? model.state.order : "asc"}
        onClick={handleSort}
      >
        {label}
        {active ? (
          <Box
            component="span"
            sx={{ position: "absolute", width: 1, height: 1, overflow: "hidden" }}
          >
            {model.state.order === "desc" ? "，降序" : "，升序"}
          </Box>
        ) : null}
      </TableSortLabel>
    </TableCell>
  );
}

/** 从一行同版本归属中选择申万 2021 三级行业。 */
function swIndustry(record: EquitySearchRecord): string {
  return (
    record.memberships.find(
      /** 只把真实 SW2021_L3 membership 作为默认申万行业。 */
      (membership) => membership.scheme === "SW2021_L3",
    )?.name ?? "—"
  );
}

/** 为字段空值附加受控原因，不让破折号隐藏数据缺口。 */
function NullableValue({
  value,
  reason,
  align = "right",
}: {
  value: string;
  reason: string | null | undefined;
  align?: "left" | "right";
}) {
  if (value !== "—") {
    return <>{value}</>;
  }

  return (
    <Tooltip title={nullReasonLabel(reason)}>
      <Typography
        component="span"
        variant="body2"
        color="text.disabled"
        sx={{ display: "inline-block", width: "100%", textAlign: align }}
      >
        —
      </Typography>
    </Tooltip>
  );
}

/** 渲染一页同一 discovery publication 的真实证券记录。 */
export function EquityMarketTable({ model }: { model: EquityMarketModel }) {
  const response = model.response;
  if (response === undefined) return null;

  return (
    <Box component="section" aria-label="股票发现结果">
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
        <Typography variant="body2" color="text.secondary">
          已加载 {response.records.length} 只证券 · 第 {model.state.page} 页 · 无昂贵总数推算
        </Typography>
        <Typography variant="caption" color="text.secondary">
          空值固定排在末尾；金额单位人民币；资金流方法学未验证，当前不开放
        </Typography>
      </Stack>
      <TableContainer sx={{ borderRadius: 2, bgcolor: "background.paper", boxShadow: 1 }}>
        <Table size="small" sx={{ minWidth: 1200 }} aria-label="A 股证券发现列表">
          <TableHead>
            <TableRow>
              <SortableHead field="symbol" label="证券" model={model} align="left" />
              <TableCell>交易所</TableCell>
              <TableCell>状态</TableCell>
              <TableCell>申万三级</TableCell>
              <SortableHead field="close" label="收盘价" model={model} />
              <SortableHead field="changePercent" label="涨跌幅" model={model} />
              <SortableHead field="amountCny" label="成交额" model={model} />
              <SortableHead field="turnoverRate" label="换手率" model={model} />
              <SortableHead field="totalMarketCap" label="总市值" model={model} />
              <SortableHead field="peTtm" label="PE TTM" model={model} />
              <SortableHead field="pb" label="PB" model={model} />
            </TableRow>
          </TableHead>
          <TableBody>
            {/* 每行只渲染 API 已验证字段；缺值保留 reasonCode，不做客户端补算。 */}
            {response.records.map((record) => {
              const direction = marketDirection(record.market.changePercent);
              const route =
                `/market/equities/${record.identity.exchange}/${record.identity.symbol}` +
                `?asOf=${record.identity.identityAsOf}`;

              return (
                <TableRow
                  hover
                  key={`${record.identity.exchange}:${record.identity.symbol}:${record.identity.identityAsOf}`}
                >
                  <TableCell
                    sx={{
                      position: "sticky",
                      left: 0,
                      zIndex: 1,
                      bgcolor: "background.paper",
                      minWidth: 168,
                    }}
                  >
                    <Link
                      component={RouterLink}
                      to={route}
                      color="text.primary"
                      underline="hover"
                      sx={{ fontWeight: 700 }}
                    >
                      {record.identity.name}
                    </Link>
                    <Typography variant="caption" color="text.secondary" sx={{ ml: 0.75 }}>
                      {record.identity.symbol}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      color="primary"
                      variant="outlined"
                      label={exchangeLabel(record.identity.exchange)}
                    />
                  </TableCell>
                  <TableCell sx={{ minWidth: 124 }}>
                    <Stack spacing={0.5} alignItems="flex-start">
                      <Chip
                        size="small"
                        color={record.statuses.listingStatus === "LISTED" ? "success" : "warning"}
                        label={listingStatusLabel(record.statuses.listingStatus)}
                      />
                      {record.statuses.tradingStatus === "TRADED" ? null : (
                        <Typography variant="caption" color="warning.dark">
                          {tradingStatusLabel(record.statuses.tradingStatus)}
                        </Typography>
                      )}
                    </Stack>
                  </TableCell>
                  <TableCell>{swIndustry(record)}</TableCell>
                  <TableCell align="right">
                    <NullableValue
                      value={formatDecimal(record.market.close)}
                      reason={record.market.nullReason}
                    />
                  </TableCell>
                  <TableCell align="right">
                    <Typography variant="body2" sx={{ color: direction.color, fontWeight: 700 }}>
                      {direction.label}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    <NullableValue
                      value={formatCny(record.market.amountCny)}
                      reason={record.market.nullReason}
                    />
                  </TableCell>
                  <TableCell align="right">
                    <NullableValue
                      value={
                        record.market.turnoverRate === null ||
                        record.market.turnoverRate === undefined
                          ? "—"
                          : `${formatDecimal(record.market.turnoverRate)}%`
                      }
                      reason={record.market.nullReason}
                    />
                  </TableCell>
                  <TableCell align="right">
                    <NullableValue
                      value={formatCny(record.capitalization.totalMarketCapCny)}
                      reason={record.capitalization.nullReason}
                    />
                  </TableCell>
                  <TableCell align="right">
                    <NullableValue
                      value={formatDecimal(record.valuation.peTtm)}
                      reason={record.valuation.nullReason}
                    />
                  </TableCell>
                  <TableCell align="right">
                    <NullableValue
                      value={formatDecimal(record.valuation.pb)}
                      reason={record.valuation.nullReason}
                    />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        <Stack
          direction="row"
          justifyContent="space-between"
          alignItems="center"
          sx={{ px: 2, py: 1.5 }}
        >
          <Typography variant="caption" color="text.secondary">
            dataVersion {response.release?.dataVersion ?? "—"} · 目标交易日{" "}
            {response.release?.effectiveAsOf ?? "—"}
          </Typography>
          <Stack direction="row" spacing={1}>
            <Button
              size="small"
              variant="outlined"
              onClick={model.firstPage}
              disabled={model.state.page === 1}
            >
              回到第一页
            </Button>
            <Button
              size="small"
              variant="contained"
              onClick={model.nextPage}
              disabled={response.page.nextCursor === null || model.searchQuery.isFetching}
            >
              下一页
            </Button>
          </Stack>
        </Stack>
      </TableContainer>
    </Box>
  );
}
