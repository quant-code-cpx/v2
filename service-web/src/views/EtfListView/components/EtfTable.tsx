import {
  ArrowForwardRounded as ArrowForwardRoundedIcon,
  FirstPageRounded as FirstPageRoundedIcon,
} from "@mui/icons-material";
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
  Typography,
} from "@mui/material";
import type { ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";

import type { EtfListFilters, EtfProfileValues, MarketDataPage } from "../../../types/etf";
import {
  etfExchangeLabel,
  etfListingStatusLabel,
  formatEtfDate,
} from "../../../utils/etf-presentation";

/** 一个可排序 ETF 表头的受控字段。 */
interface SortableEtfHeaderProps {
  field: EtfListFilters["sort"];
  filters: EtfListFilters;
  onSortChange: (field: EtfListFilters["sort"], order: EtfListFilters["order"]) => void;
  children: ReactNode;
}

/** 渲染字段白名单内的 ETF 排序表头。 */
function SortableEtfHeader({ field, filters, onSortChange, children }: SortableEtfHeaderProps) {
  const active = filters.sort === field;

  /** 当前字段再次点击切换方向，新字段从升序开始。 */
  function handleSort(): void {
    onSortChange(field, active && filters.order === "asc" ? "desc" : "asc");
  }

  return (
    <TableCell sortDirection={active ? filters.order : false}>
      <TableSortLabel
        active={active}
        direction={active ? filters.order : "asc"}
        onClick={handleSort}
      >
        {children}
      </TableSortLabel>
    </TableCell>
  );
}

/** ETF 产品目录表格所需的 page、URL 状态与游标动作。 */
interface EtfTableProps {
  page: MarketDataPage<EtfProfileValues>;
  filters: EtfListFilters;
  isUpdating: boolean;
  onSortChange: (field: EtfListFilters["sort"], order: EtfListFilters["order"]) => void;
  onNextPage: (cursor: string) => void;
  onRestart: () => void;
}

/** 展示真实 ETF 产品目录，不把目录缺席或未知状态解释为退市。 */
export function EtfTable({
  page,
  filters,
  isUpdating,
  onSortChange,
  onNextPage,
  onRestart,
}: EtfTableProps) {
  const nextCursor = page.meta.page.nextCursor;

  /** 使用当前响应的 opaque cursor 进入同一查询指纹下一页。 */
  function handleNextPage(): void {
    if (nextCursor !== null) {
      onNextPage(nextCursor);
    }
  }

  return (
    <Stack spacing={2}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="body2" color="text.secondary">
          第 {filters.page} 页 · 本页 {page.records.length} 项{isUpdating ? " · 正在更新" : ""}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          目录缺席不代表退市；上市状态仅使用来源明确字段
        </Typography>
      </Stack>
      <TableContainer sx={{ borderRadius: 2, overflow: "hidden" }}>
        <Table aria-label="ETF 产品目录" size="small">
          <TableHead>
            <TableRow>
              <SortableEtfHeader field="symbol" filters={filters} onSortChange={onSortChange}>
                代码
              </SortableEtfHeader>
              <SortableEtfHeader field="displayName" filters={filters} onSortChange={onSortChange}>
                名称
              </SortableEtfHeader>
              <TableCell>交易所</TableCell>
              <TableCell>上市状态</TableCell>
              <TableCell>ETF 类型</TableCell>
              <TableCell>基金管理人</TableCell>
              <TableCell>上市日期</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {/* 记录已经过 v2 envelope 和 values 严格校验，可安全投影到表格。 */}
            {page.records.map((record) => {
              const profile = record.values;

              return (
                <TableRow key={record.recordRef} hover>
                  <TableCell sx={{ fontVariantNumeric: "tabular-nums" }}>
                    <Link
                      component={RouterLink}
                      to={`/market/etfs/${profile.exchange}/${profile.symbol}`}
                      underline="hover"
                      fontWeight={700}
                    >
                      {profile.symbol}
                    </Link>
                  </TableCell>
                  <TableCell>{profile.displayName}</TableCell>
                  <TableCell>{etfExchangeLabel(profile.exchange)}</TableCell>
                  <TableCell>
                    <Chip
                      size="small"
                      variant="outlined"
                      color={
                        profile.listingStatus === "DELISTED"
                          ? "default"
                          : profile.listingStatus === "UNKNOWN"
                            ? "warning"
                            : "primary"
                      }
                      label={etfListingStatusLabel(profile.listingStatus)}
                    />
                  </TableCell>
                  <TableCell>{profile.etfType}</TableCell>
                  <TableCell>{profile.managerName ?? "未披露"}</TableCell>
                  <TableCell>{formatEtfDate(profile.listedOn)}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableContainer>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Button
          color="inherit"
          startIcon={<FirstPageRoundedIcon />}
          disabled={filters.page === 1}
          onClick={onRestart}
        >
          返回第一页
        </Button>
        <Box>
          <Button
            variant="outlined"
            endIcon={<ArrowForwardRoundedIcon />}
            disabled={!page.meta.page.hasMore || nextCursor === null || isUpdating}
            onClick={handleNextPage}
          >
            下一页
          </Button>
        </Box>
      </Stack>
    </Stack>
  );
}
