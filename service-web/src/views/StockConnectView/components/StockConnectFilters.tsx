import { Button, FormControl, InputLabel, MenuItem, Select, Stack, TextField } from "@mui/material";
import { useCallback } from "react";
import type { ChangeEvent } from "react";
import type { SelectChangeEvent } from "@mui/material";

import type { StockConnectDateUrlValue, StockConnectTrendDays } from "../utils/stock-connect-url";
import { stockConnectTrendDayOptions } from "../utils/stock-connect-url";

/** 描述交易日筛选的受控状态。 */
interface StockConnectDateFilterProps {
  value: StockConnectDateUrlValue;
  onChange: (value: StockConnectDateUrlValue) => void;
}

/** 显示 latest 与精确交易日选择，清空日期即恢复 latest。 */
export function StockConnectDateFilter({ value, onChange }: StockConnectDateFilterProps) {
  /** 将原生日历输入转换为精确日期或 latest。 */
  const handleDateChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      onChange(
        event.target.value.length === 0
          ? "latest"
          : (event.target.value as StockConnectDateUrlValue),
      );
    },
    [onChange],
  );

  /** 显式恢复 latest 语义，不把 resolvedTradeDate 写进 URL。 */
  const handleLatest = useCallback(() => {
    onChange("latest");
  }, [onChange]);

  return (
    <Stack direction="row" spacing={1}>
      <TextField
        label="交易日"
        type="date"
        value={value === "latest" ? "" : value}
        onChange={handleDateChange}
        slotProps={{ inputLabel: { shrink: true } }}
        sx={{ width: 180 }}
      />
      <Button variant={value === "latest" ? "contained" : "outlined"} onClick={handleLatest}>
        latest
      </Button>
    </Stack>
  );
}

/** 描述交易日趋势窗口的受控状态。 */
interface StockConnectTrendDaysSelectProps {
  value: StockConnectTrendDays;
  onChange: (value: StockConnectTrendDays) => void;
}

/** 选择合同允许的交易日窗口，不接受自然日或任意数值。 */
export function StockConnectTrendDaysSelect({ value, onChange }: StockConnectTrendDaysSelectProps) {
  /** 把 MUI Select 字符串安全转换为冻结交易日枚举。 */
  const handleChange = useCallback(
    (event: SelectChangeEvent<string>) => {
      onChange(Number(event.target.value) as StockConnectTrendDays);
    },
    [onChange],
  );

  return (
    <FormControl sx={{ width: 150 }}>
      <InputLabel id="stock-connect-trend-days-label">趋势窗口</InputLabel>
      <Select
        labelId="stock-connect-trend-days-label"
        label="趋势窗口"
        value={String(value)}
        onChange={handleChange}
      >
        {stockConnectTrendDayOptions.map(
          /** 渲染受合同约束的交易日窗口。 */
          (days) => (
            <MenuItem key={days} value={String(days)}>
              {days} 个交易日
            </MenuItem>
          ),
        )}
      </Select>
    </FormControl>
  );
}
