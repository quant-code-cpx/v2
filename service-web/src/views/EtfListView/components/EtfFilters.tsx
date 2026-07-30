import { SearchRounded as SearchRoundedIcon } from "@mui/icons-material";
import { Box, Button, FormControl, InputLabel, MenuItem, Select, TextField } from "@mui/material";
import type { FormEvent } from "react";
import type { SelectChangeEvent } from "@mui/material";

import type { EtfExchange, EtfListFilters } from "../../../types/etf";

/** ETF 目录筛选带需要的 URL 状态与动作。 */
interface EtfFiltersProps {
  filters: EtfListFilters;
  onApply: (changes: Partial<Pick<EtfListFilters, "exchange" | "q">>) => void;
  onReset: () => void;
}

/** 渲染只对应真实 typed-reader 能力的 ETF 筛选控件。 */
export function EtfFilters({ filters, onApply, onReset }: EtfFiltersProps) {
  /** 提交代码前缀或名称包含关键词，空白提交表示清除关键词。 */
  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const rawQuery = form.get("q");
    const q = typeof rawQuery === "string" ? rawQuery.trim().slice(0, 40) : "";

    onApply({ q: q.length > 0 ? q : undefined });
  }

  /** 切换交易所 publication 分区，并从第一页重新查询。 */
  function handleExchangeChange(event: SelectChangeEvent): void {
    onApply({ exchange: event.target.value as EtfExchange });
  }

  return (
    <Box
      component="form"
      role="search"
      aria-label="ETF 目录筛选"
      onSubmit={handleSubmit}
      sx={{
        display: "grid",
        gridTemplateColumns: "220px minmax(320px, 1fr) auto auto",
        gap: 2,
        alignItems: "start",
        p: 2.5,
        borderRadius: 2,
        bgcolor: "grey.100",
      }}
    >
      <FormControl>
        <InputLabel id="etf-exchange-label">交易所</InputLabel>
        <Select
          labelId="etf-exchange-label"
          label="交易所"
          value={filters.exchange}
          onChange={handleExchangeChange}
        >
          <MenuItem value="SSE">上海证券交易所</MenuItem>
          <MenuItem value="SZSE">深圳证券交易所</MenuItem>
        </Select>
      </FormControl>
      <TextField
        key={filters.q ?? "empty-query"}
        name="q"
        label="代码或名称"
        defaultValue={filters.q ?? ""}
        placeholder="纯数字匹配代码前缀，其他文字匹配名称"
        inputProps={{ maxLength: 40 }}
      />
      <Button type="submit" variant="contained" startIcon={<SearchRoundedIcon />} sx={{ mt: 1.25 }}>
        查询
      </Button>
      <Button type="button" color="inherit" onClick={onReset} sx={{ mt: 1.25 }}>
        重置
      </Button>
    </Box>
  );
}
