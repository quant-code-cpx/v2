import { Box, Button, MenuItem, Select, Stack, TextField, Typography } from "@mui/material";
import type { SelectChangeEvent } from "@mui/material";

import type {
  EquityExchange,
  EquityListingStatus,
  EquitySearchSortField,
  EquityTradingStatus,
} from "../../../types/equity-market";
import type { EquityMarketModel } from "../hooks/useEquityMarket";

/** 固定交易所选项。 */
const exchangeOptions: Array<{ value: EquityExchange; label: string }> = [
  { value: "SSE", label: "上交所" },
  { value: "SZSE", label: "深交所" },
  { value: "BSE", label: "北交所" },
];

/** 固定上市生命周期选项。 */
const listingOptions: Array<{ value: EquityListingStatus; label: string; disabled?: boolean }> = [
  { value: "LISTED", label: "上市" },
  { value: "SUSPENDED", label: "暂停上市（数据源未覆盖）", disabled: true },
  { value: "DELISTED", label: "退市" },
];

/** 固定普通交易状态选项。 */
const tradingOptions: Array<{ value: EquityTradingStatus; label: string }> = [
  { value: "TRADED", label: "正常交易" },
  { value: "TRADE_SUSPENDED", label: "停牌" },
  { value: "NO_SESSION", label: "非交易日" },
  { value: "NOT_APPLICABLE", label: "不适用" },
  { value: "UNKNOWN", label: "尚未证实" },
];

/** 固定服务端可索引排序选项。 */
const sortOptions: Array<{ value: EquitySearchSortField; label: string }> = [
  { value: "symbol", label: "按代码" },
  { value: "changePercent", label: "按涨跌幅" },
  { value: "amountCny", label: "按成交额" },
  { value: "turnoverRate", label: "按换手率" },
  { value: "totalMarketCap", label: "按总市值" },
  { value: "peTtm", label: "按 PE TTM" },
];

/** 渲染股票中心桌面筛选带；所有业务状态直接写入 URL。 */
export function EquityMarketFilters({ model }: { model: EquityMarketModel }) {
  /** 从 MUI 多选值读取交易所白名单。 */
  const handleExchangeChange = (event: SelectChangeEvent<EquityExchange[]>) => {
    model.setExchanges(event.target.value as EquityExchange[]);
  };

  /** 从 MUI 多选值读取上市生命周期白名单。 */
  const handleListingChange = (event: SelectChangeEvent<EquityListingStatus[]>) => {
    model.setListingStatuses(event.target.value as EquityListingStatus[]);
  };

  /** 从 MUI 多选值读取普通交易状态白名单。 */
  const handleTradingChange = (event: SelectChangeEvent<EquityTradingStatus[]>) => {
    model.setTradingStatuses(event.target.value as EquityTradingStatus[]);
  };

  return (
    <Box
      component="section"
      aria-label="股票筛选"
      sx={{
        p: 2,
        borderRadius: 2,
        bgcolor: "background.paper",
        boxShadow: 1,
      }}
    >
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "minmax(260px, 2fr) repeat(3, minmax(150px, 1fr)) auto",
          gap: 1.5,
          alignItems: "start",
        }}
      >
        <TextField
          label="股票代码、名称或交易所"
          value={model.state.q ?? ""}
          onChange={model.setQuery}
          inputProps={{ maxLength: 64 }}
        />
        <Select
          multiple
          displayEmpty
          aria-label="交易所"
          value={model.state.exchanges}
          onChange={handleExchangeChange}
          renderValue={(values) =>
            values.length === 0
              ? "全部交易所"
              : exchangeOptions
                  .filter((option) => values.includes(option.value))
                  .map((option) => option.label)
                  .join("、")
          }
        >
          {exchangeOptions.map((option) => (
            <MenuItem key={option.value} value={option.value}>
              {option.label}
            </MenuItem>
          ))}
        </Select>
        <Select
          multiple
          displayEmpty
          aria-label="上市生命周期"
          value={model.state.listingStatuses}
          onChange={handleListingChange}
          renderValue={(values) =>
            values.length === 0
              ? "默认上市状态"
              : listingOptions
                  .filter((option) => values.includes(option.value))
                  .map((option) => option.label)
                  .join("、")
          }
        >
          {listingOptions.map((option) => (
            <MenuItem key={option.value} value={option.value} disabled={option.disabled}>
              {option.label}
            </MenuItem>
          ))}
        </Select>
        <Select
          multiple
          displayEmpty
          aria-label="普通交易状态"
          value={model.state.tradingStatuses}
          onChange={handleTradingChange}
          renderValue={(values) =>
            values.length === 0
              ? "全部交易状态"
              : tradingOptions
                  .filter((option) => values.includes(option.value))
                  .map((option) => option.label)
                  .join("、")
          }
        >
          {tradingOptions.map((option) => (
            <MenuItem key={option.value} value={option.value}>
              {option.label}
            </MenuItem>
          ))}
        </Select>
        <Stack direction="row" spacing={1}>
          <Button variant="outlined" onClick={model.reset}>
            重置
          </Button>
          <Button
            variant="contained"
            onClick={model.refresh}
            disabled={model.searchQuery.isFetching}
          >
            {model.searchQuery.isFetching ? "复验中" : "刷新"}
          </Button>
        </Stack>
      </Box>

      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mt: 1.5 }}>
        <Typography variant="body2" color="text.secondary" sx={{ width: 72, flexShrink: 0 }}>
          分类代码
        </Typography>
        <TextField
          size="small"
          label="Eastmoney 行业"
          value={model.state.industries[0] ?? ""}
          onChange={model.setIndustry}
          sx={{ width: 220 }}
        />
        <TextField
          size="small"
          label="Eastmoney 概念"
          value={model.state.concepts[0] ?? ""}
          onChange={model.setConcept}
          sx={{ width: 220 }}
        />
        <TextField
          size="small"
          label="申万 2021 三级"
          value={model.state.swIndustries[0] ?? ""}
          onChange={model.setSwIndustry}
          sx={{ width: 220 }}
        />
        <Select
          size="small"
          aria-label="列表排序字段"
          value={model.state.sort}
          onChange={(event) => {
            // 选择明确排序字段后，服务端负责全市场排序并固定空值 LAST。
            model.setSort(event.target.value as EquitySearchSortField);
          }}
          sx={{ width: 180, ml: "auto" }}
        >
          {sortOptions.map((option) => (
            <MenuItem key={option.value} value={option.value}>
              {option.label}
            </MenuItem>
          ))}
        </Select>
      </Stack>
    </Box>
  );
}
