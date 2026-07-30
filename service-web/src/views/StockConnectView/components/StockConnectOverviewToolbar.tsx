import {
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
} from "@mui/material";
import { useCallback } from "react";
import type { MouseEvent } from "react";
import type { SelectChangeEvent } from "@mui/material";

import {
  stockConnectChannelDescription,
  stockConnectChannelLabel,
} from "../utils/stock-connect-presentation";
import {
  stockConnectChannelSlugByCode,
  stockConnectChannelsForDirection,
} from "../utils/stock-connect-url";
import type {
  StockConnectChannelSlug,
  StockConnectDateUrlValue,
  StockConnectDirectionFilter,
  StockConnectTrendDays,
} from "../utils/stock-connect-url";
import { StockConnectDateFilter, StockConnectTrendDaysSelect } from "./StockConnectFilters";

/** 描述总览页全部可分享筛选。 */
interface StockConnectOverviewToolbarProps {
  date: StockConnectDateUrlValue;
  direction: StockConnectDirectionFilter;
  channel: StockConnectChannelSlug;
  trendDays: StockConnectTrendDays;
  onDateChange: (date: StockConnectDateUrlValue) => void;
  onDirectionChange: (direction: StockConnectDirectionFilter) => void;
  onChannelChange: (channel: StockConnectChannelSlug) => void;
  onTrendDaysChange: (days: StockConnectTrendDays) => void;
}

/** 渲染总览方向、趋势通道、交易日和交易日窗口筛选。 */
export function StockConnectOverviewToolbar({
  date,
  direction,
  channel,
  trendDays,
  onDateChange,
  onDirectionChange,
  onChannelChange,
  onTrendDaysChange,
}: StockConnectOverviewToolbarProps) {
  const visibleChannels = stockConnectChannelsForDirection(direction);

  /** 忽略 ToggleButtonGroup 取消选择产生的 null，保证方向始终有效。 */
  const handleDirectionChange = useCallback(
    (_event: MouseEvent<HTMLElement>, value: StockConnectDirectionFilter | null) => {
      if (value !== null) {
        onDirectionChange(value);
      }
    },
    [onDirectionChange],
  );

  /** 将趋势通道 Select 更新为 URL 短名。 */
  const handleChannelChange = useCallback(
    (event: SelectChangeEvent<string>) => {
      onChannelChange(event.target.value as StockConnectChannelSlug);
    },
    [onChannelChange],
  );

  return (
    <Stack direction="row" spacing={1.5} alignItems="center">
      <ToggleButtonGroup
        exclusive
        value={direction}
        onChange={handleDirectionChange}
        size="small"
        aria-label="互联互通业务方向"
      >
        <ToggleButton value="all">全部方向</ToggleButton>
        <ToggleButton value="northbound">北向</ToggleButton>
        <ToggleButton value="southbound">南向</ToggleButton>
      </ToggleButtonGroup>
      <StockConnectDateFilter value={date} onChange={onDateChange} />
      <FormControl sx={{ width: 210 }}>
        <InputLabel id="stock-connect-trend-channel-label">趋势与榜单通道</InputLabel>
        <Select
          labelId="stock-connect-trend-channel-label"
          label="趋势与榜单通道"
          value={channel}
          onChange={handleChannelChange}
        >
          {visibleChannels.map(
            /** 只渲染当前方向允许的通道，金额仍逐通道展示。 */
            (channelCode) => {
              const slug = stockConnectChannelSlugByCode[channelCode];
              return (
                <MenuItem key={slug} value={slug}>
                  {stockConnectChannelLabel(channelCode)} ·{" "}
                  {stockConnectChannelDescription(channelCode)}
                </MenuItem>
              );
            },
          )}
        </Select>
      </FormControl>
      <StockConnectTrendDaysSelect value={trendDays} onChange={onTrendDaysChange} />
    </Stack>
  );
}
