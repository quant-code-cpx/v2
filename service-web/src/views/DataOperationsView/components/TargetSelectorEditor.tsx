import {
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  type SelectChangeEvent,
} from "@mui/material";
import { useCallback } from "react";
import type { ChangeEvent } from "react";

import type {
  MarginTargetSelector,
  MoneyFlowTargetSelector,
  TargetSelector,
  TargetSelectorKind,
} from "../../../types/data-operations";
import { createTargetSelector, targetSelectorKindLabel } from "../utils/target-selector";

/** 选择器字段名只覆盖合同 `TargetSelector` 的受控属性。 */
type SelectorField =
  | "exchange"
  | "symbol"
  | "scheme"
  | "sectorCode"
  | "venue"
  | "contract"
  | "operation"
  | "scope"
  | "etf"
  | "channel"
  | "direction"
  | "indexCode"
  | "methodology"
  | "window"
  | "sectorType";

/** 描述受控 selector 编辑器的合同输入、允许类别和变更出口。 */
interface TargetSelectorEditorProps {
  idPrefix: string;
  datasetCode: string;
  selector: TargetSelector;
  selectorKinds: TargetSelectorKind[];
  onChange: (selector: TargetSelector) => void;
  disabled?: boolean;
}

/** 将文本转换为受限证券、ETF、期货、指数或资金流代码的规范大写形式。 */
function normalizeCode(value: string): string {
  return value.trim().toUpperCase();
}

/** 按日频资金流范围创建不携带跨范围字段的严格最小 selector。 */
function createDailyMoneyFlowSelector(scope: string): MoneyFlowTargetSelector | undefined {
  if (scope === "EQUITY") {
    return {
      kind: "MONEY_FLOW",
      operation: "DAILY",
      scope,
      exchange: "SSE",
      symbol: "",
    };
  }
  if (scope === "SECTOR") {
    return {
      kind: "MONEY_FLOW",
      operation: "DAILY",
      scope,
      scheme: "eastmoney.industry",
      sectorCode: "",
    };
  }
  return scope === "MARKET" ? { kind: "MONEY_FLOW", operation: "DAILY", scope } : undefined;
}

/** 按东财排行范围创建与方法学窗口匹配的严格最小 selector。 */
function createEastmoneyRankingMoneyFlowSelector(
  scope: string,
): MoneyFlowTargetSelector | undefined {
  if (scope === "EQUITY") {
    return {
      kind: "MONEY_FLOW",
      operation: "RANKING",
      methodology: "EASTMONEY_ORDER_SIZE",
      scope,
      window: "TODAY",
    };
  }
  if (scope === "SECTOR") {
    return {
      kind: "MONEY_FLOW",
      operation: "RANKING",
      methodology: "EASTMONEY_ORDER_SIZE",
      scope,
      sectorType: "INDUSTRY",
      window: "TODAY",
    };
  }
  return undefined;
}

/** 按同花顺排行范围创建与交易方向方法学匹配的严格最小 selector。 */
function createThsRankingMoneyFlowSelector(scope: string): MoneyFlowTargetSelector | undefined {
  if (scope !== "EQUITY" && scope !== "INDUSTRY" && scope !== "CONCEPT") return undefined;
  return {
    kind: "MONEY_FLOW",
    operation: "RANKING",
    methodology: "THS_TRADE_DIRECTION",
    scope,
    window: "INTRADAY",
  };
}

/** 冻结两融 operation 可选市场，避免编辑器构造 data-sync 没有执行器的组合。 */
const MARGIN_VENUES_BY_OPERATION = {
  MARKET: ["SSE", "SZSE"],
  SECURITY: ["SSE", "SZSE"],
  ELIGIBILITY: ["SZSE", "BSE"],
} as const;

/** 仅更新当前两融 operation 支持的市场，并始终清除未实现的证券子选择器。 */
function updateMarginVenue(selector: MarginTargetSelector, rawValue: string): MarginTargetSelector {
  if (selector.operation === "ELIGIBILITY") {
    return rawValue === "SZSE" || rawValue === "BSE"
      ? { kind: "MARGIN", operation: "ELIGIBILITY", venue: rawValue, security: null }
      : selector;
  }
  return rawValue === "SSE" || rawValue === "SZSE"
    ? { kind: "MARGIN", operation: selector.operation, venue: rawValue, security: null }
    : selector;
}

/** 将资金流编辑动作收敛为已冻结的 operation、方法学、范围和窗口组合。 */
function updateMoneyFlowTargetSelector(
  selector: MoneyFlowTargetSelector,
  field: SelectorField,
  rawValue: string,
): MoneyFlowTargetSelector {
  if (selector.operation === "DAILY") {
    if (field === "scope") return createDailyMoneyFlowSelector(rawValue) ?? selector;
    if (selector.scope === "EQUITY") {
      if (field === "exchange" && ["SSE", "SZSE", "BSE"].includes(rawValue)) {
        return { ...selector, exchange: rawValue as "SSE" | "SZSE" | "BSE" };
      }
      return field === "symbol" ? { ...selector, symbol: normalizeCode(rawValue) } : selector;
    }
    return selector.scope === "SECTOR" && field === "sectorCode"
      ? { ...selector, sectorCode: rawValue }
      : selector;
  }
  if (field === "methodology") {
    if (rawValue === "EASTMONEY_ORDER_SIZE") {
      return createEastmoneyRankingMoneyFlowSelector("EQUITY") ?? selector;
    }
    if (rawValue === "THS_TRADE_DIRECTION") {
      return createThsRankingMoneyFlowSelector("EQUITY") ?? selector;
    }
    return selector;
  }
  if (selector.methodology === "EASTMONEY_ORDER_SIZE") {
    if (field === "scope") return createEastmoneyRankingMoneyFlowSelector(rawValue) ?? selector;
    if (
      selector.scope === "EQUITY" &&
      field === "window" &&
      ["TODAY", "DAY_3", "DAY_5", "DAY_10"].includes(rawValue)
    ) {
      return { ...selector, window: rawValue as "TODAY" | "DAY_3" | "DAY_5" | "DAY_10" };
    }
    if (selector.scope === "SECTOR") {
      if (field === "sectorType" && ["INDUSTRY", "CONCEPT", "REGION"].includes(rawValue)) {
        return { ...selector, sectorType: rawValue as "INDUSTRY" | "CONCEPT" | "REGION" };
      }
      if (field === "window" && ["TODAY", "DAY_5", "DAY_10"].includes(rawValue)) {
        return { ...selector, window: rawValue as "TODAY" | "DAY_5" | "DAY_10" };
      }
    }
    return selector;
  }
  if (field === "scope") return createThsRankingMoneyFlowSelector(rawValue) ?? selector;
  if (field === "window" && ["INTRADAY", "DAY_3", "DAY_5", "DAY_10", "DAY_20"].includes(rawValue)) {
    return {
      ...selector,
      window: rawValue as "INTRADAY" | "DAY_3" | "DAY_5" | "DAY_10" | "DAY_20",
    };
  }
  return selector;
}

/** 将受控输入字段安全映射为合同定义的严格 selector 并集。 */
function updateTargetSelectorField(
  selector: TargetSelector,
  field: SelectorField,
  rawValue: string,
): TargetSelector {
  switch (selector.kind) {
    case "GLOBAL":
      return selector;
    case "INSTRUMENT":
      if (field === "exchange")
        return { ...selector, exchange: rawValue as "SSE" | "SZSE" | "BSE" };
      if (field === "symbol") return { ...selector, symbol: normalizeCode(rawValue) };
      return selector;
    case "SECTOR":
      if (field === "scheme") return { ...selector, scheme: rawValue };
      if (field === "sectorCode") return { ...selector, sectorCode: rawValue };
      return selector;
    case "SCHEME":
      return field === "scheme" ? { ...selector, scheme: rawValue } : selector;
    case "EXCHANGE":
      return field === "exchange"
        ? { ...selector, exchange: rawValue as "SSE" | "SZSE" | "BSE" }
        : selector;
    case "CONTRACT":
      if (field === "venue") {
        return {
          ...selector,
          venue: rawValue as "CFFEX" | "SHFE" | "DCE" | "CZCE" | "INE",
        };
      }
      if (field === "contract") return { ...selector, contract: normalizeCode(rawValue) };
      return selector;
    case "ETF":
      if (field === "operation") {
        if (rawValue === "MASTER") {
          return {
            kind: "ETF",
            operation: "MASTER",
            venue: null,
            scope: "ALL_VENUES",
            etf: null,
          };
        }
        const operation = rawValue as "STATUS" | "BARS" | "NAV";
        if (selector.operation === "MASTER") {
          return { kind: "ETF", operation, venue: selector.venue, etf: "" };
        }
        if ("scope" in selector && selector.scope === "ALL_ETFS") {
          return { ...selector, operation, profileDataVersions: null };
        }
        return {
          ...selector,
          operation,
        };
      }
      if (field === "venue") {
        if (selector.operation === "MASTER") {
          if ("scope" in selector) return selector;
          return { ...selector, venue: rawValue as "SSE" | "SZSE" };
        }
        if ("scope" in selector) return selector;
        return {
          kind: "ETF",
          operation: selector.operation,
          venue: rawValue.length === 0 ? null : (rawValue as "SSE" | "SZSE"),
          etf: selector.etf,
        };
      }
      if (field === "scope" && selector.operation === "MASTER") {
        if (rawValue === "ALL_VENUES") {
          return {
            kind: "ETF",
            operation: "MASTER",
            venue: null,
            scope: "ALL_VENUES",
            etf: null,
          };
        }
        return {
          kind: "ETF",
          operation: "MASTER",
          venue: "SSE",
          etf: null,
        };
      }
      if (field === "scope" && selector.operation !== "MASTER") {
        if (rawValue === "ALL_ETFS") {
          return {
            kind: "ETF",
            operation: selector.operation,
            venue: null,
            scope: "ALL_ETFS",
            etf: null,
            profileDataVersions: null,
          };
        }
        return {
          kind: "ETF",
          operation: selector.operation,
          venue: selector.venue,
          etf: "scope" in selector ? "" : selector.etf,
        };
      }
      if (field === "etf" && selector.operation !== "MASTER" && !("scope" in selector)) {
        return { ...selector, etf: normalizeCode(rawValue) };
      }
      return selector;
    case "MARGIN":
      return field === "venue" ? updateMarginVenue(selector, rawValue) : selector;
    case "STOCK_CONNECT":
    case "STOCK_CONNECT_RESEARCH":
      if (field === "channel") {
        return { ...selector, channel: rawValue as "ALL" | "SH" | "SZ" };
      }
      if (field === "direction") {
        return {
          ...selector,
          direction: rawValue.length === 0 ? null : (rawValue as "NORTHBOUND" | "SOUTHBOUND"),
        };
      }
      return selector;
    case "TRADING_EVENT":
      return field === "operation"
        ? { ...selector, operation: rawValue as "DRAGON_TIGER" | "BLOCK_TRADE" }
        : selector;
    case "INDEX":
      if (field === "indexCode" && selector.indexCode !== null)
        return { ...selector, indexCode: normalizeCode(rawValue) };
      return selector;
    case "MONEY_FLOW":
      return updateMoneyFlowTargetSelector(selector, field, rawValue);
  }
}

/** 仅渲染 capability 允许的 selector kind，并构造合同受限字段。 */
export function TargetSelectorEditor({
  idPrefix,
  datasetCode,
  selector,
  selectorKinds,
  onChange,
  disabled = false,
}: TargetSelectorEditorProps) {
  /** 切换服务端允许的类别，丢弃不属于新类别的旧字段。 */
  const handleKindChange = useCallback(
    (event: SelectChangeEvent) => {
      const nextKind = event.target.value as TargetSelectorKind;
      const nextSelector = createTargetSelector(nextKind, datasetCode);
      if (selectorKinds.includes(nextKind) && nextSelector !== undefined) onChange(nextSelector);
    },
    [datasetCode, onChange, selectorKinds],
  );

  /** 把一个文本输入安全映射到当前 selector 的对应字段。 */
  const createTextChangeHandler = useCallback(
    (field: SelectorField) => (event: ChangeEvent<HTMLInputElement>) => {
      onChange(updateTargetSelectorField(selector, field, event.target.value));
    },
    [onChange, selector],
  );

  /** 把一个受控下拉选项安全映射到当前 selector 的对应字段。 */
  const createSelectChangeHandler = useCallback(
    (field: SelectorField) => (event: SelectChangeEvent) => {
      onChange(updateTargetSelectorField(selector, field, event.target.value));
    },
    [onChange, selector],
  );

  return (
    <Stack spacing={1.5} sx={{ minWidth: 0 }}>
      <FormControl fullWidth disabled={disabled}>
        <InputLabel id={`${idPrefix}-selector-kind-label`}>业务范围</InputLabel>
        <Select
          labelId={`${idPrefix}-selector-kind-label`}
          label="业务范围"
          value={selector.kind}
          onChange={handleKindChange}
        >
          {/* 只显示当前数据集 capability.selectorKinds，不能填 Provider 参数或 URI。 */}
          {selectorKinds.map((kind) => (
            <MenuItem key={kind} value={kind}>
              {targetSelectorKindLabel(kind)}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      {selector.kind === "GLOBAL" ? <TextField disabled value="全数据集" label="同步范围" /> : null}
      {selector.kind === "INSTRUMENT" ? (
        <Stack direction="row" spacing={1.5}>
          <FormControl sx={{ width: 120 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-instrument-exchange-label`}>交易所</InputLabel>
            <Select
              labelId={`${idPrefix}-instrument-exchange-label`}
              label="交易所"
              value={selector.exchange}
              onChange={createSelectChangeHandler("exchange")}
            >
              <MenuItem value="SSE">SSE</MenuItem>
              <MenuItem value="SZSE">SZSE</MenuItem>
              <MenuItem value="BSE">BSE</MenuItem>
            </Select>
          </FormControl>
          <TextField
            required
            fullWidth
            label="证券代码"
            value={selector.symbol}
            onChange={createTextChangeHandler("symbol")}
            disabled={disabled}
            inputProps={{ maxLength: 32 }}
          />
        </Stack>
      ) : null}
      {selector.kind === "SECTOR" ? (
        <Stack direction="row" spacing={1.5}>
          <TextField
            required
            fullWidth
            label="分类体系"
            value={selector.scheme}
            onChange={createTextChangeHandler("scheme")}
            disabled={disabled}
            inputProps={{ maxLength: 64 }}
          />
          <TextField
            required
            fullWidth
            label="行业代码"
            value={selector.sectorCode}
            onChange={createTextChangeHandler("sectorCode")}
            disabled={disabled}
            inputProps={{ maxLength: 120 }}
          />
        </Stack>
      ) : null}
      {selector.kind === "SCHEME" ? (
        <TextField
          required
          fullWidth
          label="分类体系"
          value={selector.scheme}
          onChange={createTextChangeHandler("scheme")}
          disabled={disabled}
          inputProps={{ maxLength: 64 }}
        />
      ) : null}
      {selector.kind === "EXCHANGE" ? (
        <FormControl fullWidth disabled={disabled}>
          <InputLabel id={`${idPrefix}-exchange-label`}>交易所</InputLabel>
          <Select
            labelId={`${idPrefix}-exchange-label`}
            label="交易所"
            value={selector.exchange}
            onChange={createSelectChangeHandler("exchange")}
          >
            <MenuItem value="SSE">SSE</MenuItem>
            <MenuItem value="SZSE">SZSE</MenuItem>
            <MenuItem value="BSE">BSE</MenuItem>
          </Select>
        </FormControl>
      ) : null}
      {selector.kind === "CONTRACT" ? (
        <Stack direction="row" spacing={1.5}>
          <FormControl sx={{ width: 140 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-contract-venue-label`}>交易场所</InputLabel>
            <Select
              labelId={`${idPrefix}-contract-venue-label`}
              label="交易场所"
              value={selector.venue}
              onChange={createSelectChangeHandler("venue")}
            >
              {(["CFFEX", "SHFE", "DCE", "CZCE", "INE"] as const).map((venue) => (
                <MenuItem key={venue} value={venue}>
                  {venue}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <TextField
            required
            fullWidth
            label="合约代码"
            value={selector.contract}
            onChange={createTextChangeHandler("contract")}
            disabled={disabled}
            inputProps={{ maxLength: 64 }}
          />
        </Stack>
      ) : null}
      {selector.kind === "ETF" ? (
        <Stack direction="row" spacing={1.5}>
          <TextField
            sx={{ width: 140 }}
            label="ETF 数据能力"
            value={selector.operation}
            disabled
            helperText="由数据集固定"
          />
          <FormControl sx={{ width: 190 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-etf-scope-label`}>
              {selector.operation === "MASTER" ? "主数据同步范围" : "ETF 同步范围"}
            </InputLabel>
            <Select
              labelId={`${idPrefix}-etf-scope-label`}
              label={selector.operation === "MASTER" ? "主数据同步范围" : "ETF 同步范围"}
              value={
                selector.operation === "MASTER"
                  ? "scope" in selector
                    ? "ALL_VENUES"
                    : "ONE_VENUE"
                  : "scope" in selector
                    ? "ALL_ETFS"
                    : "ONE"
              }
              onChange={createSelectChangeHandler("scope")}
            >
              {selector.operation === "MASTER" ? (
                <MenuItem value="ONE_VENUE">单市场</MenuItem>
              ) : (
                <MenuItem value="ONE">单只 ETF</MenuItem>
              )}
              {selector.operation === "MASTER" ? (
                <MenuItem value="ALL_VENUES">沪深全市场</MenuItem>
              ) : (
                <MenuItem value="ALL_ETFS">全部已发布 ETF</MenuItem>
              )}
            </Select>
          </FormControl>
          {!("scope" in selector) ? (
            <FormControl sx={{ width: 140 }} disabled={disabled}>
              <InputLabel id={`${idPrefix}-etf-venue-label`}>
                {selector.operation === "MASTER" ? "交易所" : "场所校验"}
              </InputLabel>
              <Select
                labelId={`${idPrefix}-etf-venue-label`}
                label={selector.operation === "MASTER" ? "交易所" : "场所校验"}
                value={selector.venue ?? ""}
                onChange={createSelectChangeHandler("venue")}
              >
                {selector.operation !== "MASTER" ? (
                  <MenuItem value="">场所由显式 qualified identity 给出</MenuItem>
                ) : null}
                <MenuItem value="SSE">SSE</MenuItem>
                <MenuItem value="SZSE">SZSE</MenuItem>
              </Select>
            </FormControl>
          ) : null}
          {selector.operation !== "MASTER" &&
          !("scope" in selector && selector.scope === "ALL_ETFS") ? (
            <TextField
              required
              fullWidth
              label="ETF 代码"
              placeholder="SSE.510300"
              value={selector.etf}
              onChange={createTextChangeHandler("etf")}
              disabled={disabled}
              inputProps={{ maxLength: 11 }}
            />
          ) : null}
          {selector.operation !== "MASTER" &&
          "scope" in selector &&
          selector.scope === "ALL_ETFS" ? (
            <TextField
              fullWidth
              label="ETF 集合"
              value="全部已发布 ETF"
              disabled
              helperText="预检将冻结 SSE、SZSE 当前已发布的 ETF profile publication。"
            />
          ) : null}
          {selector.operation === "MASTER" &&
          "scope" in selector &&
          selector.scope === "ALL_VENUES" ? (
            <TextField
              fullWidth
              label="主数据市场"
              value="SSE + SZSE"
              disabled
              helperText="同步服务将显式拆分沪深两市并分别发布 profile。"
            />
          ) : null}
        </Stack>
      ) : null}
      {selector.kind === "MARGIN" ? (
        <Stack direction="row" spacing={1.5}>
          <TextField
            sx={{ width: 160 }}
            label="两融数据能力"
            value={selector.operation}
            disabled
            helperText="由数据集固定"
          />
          <FormControl sx={{ width: 120 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-margin-venue-label`}>市场</InputLabel>
            <Select
              labelId={`${idPrefix}-margin-venue-label`}
              label="市场"
              value={selector.venue}
              onChange={createSelectChangeHandler("venue")}
            >
              {MARGIN_VENUES_BY_OPERATION[selector.operation].map((venue) => (
                <MenuItem key={venue} value={venue}>
                  {venue}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </Stack>
      ) : null}
      {selector.kind === "STOCK_CONNECT" ? (
        <Stack direction="row" spacing={1.5}>
          <TextField
            sx={{ flex: 1 }}
            disabled
            label="操作"
            value="完整互联互通数据包"
            helperText="市场统计与来源活跃证券同批发布"
          />
          <FormControl sx={{ flex: 1 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-connect-channel-label`}>通道</InputLabel>
            <Select
              labelId={`${idPrefix}-connect-channel-label`}
              label="通道"
              value={selector.channel}
              onChange={createSelectChangeHandler("channel")}
            >
              <MenuItem value="ALL">全部通道</MenuItem>
              <MenuItem value="SH">沪通道</MenuItem>
              <MenuItem value="SZ">深通道</MenuItem>
            </Select>
          </FormControl>
          <FormControl sx={{ flex: 1 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-connect-direction-label`}>方向</InputLabel>
            <Select
              labelId={`${idPrefix}-connect-direction-label`}
              label="方向"
              value={selector.direction ?? ""}
              onChange={createSelectChangeHandler("direction")}
            >
              <MenuItem value="">全部方向</MenuItem>
              <MenuItem value="NORTHBOUND">NORTHBOUND</MenuItem>
              <MenuItem value="SOUTHBOUND">SOUTHBOUND</MenuItem>
            </Select>
          </FormControl>
        </Stack>
      ) : null}
      {selector.kind === "STOCK_CONNECT_RESEARCH" ? (
        <Stack direction="row" spacing={1.5}>
          <TextField
            sx={{ flex: 1 }}
            disabled
            label="操作"
            value="港通市场统计（research）"
            helperText="仅供 research；不产生正式 publication"
          />
          <FormControl sx={{ flex: 1 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-connect-research-channel-label`}>研究通道</InputLabel>
            <Select
              labelId={`${idPrefix}-connect-research-channel-label`}
              label="研究通道"
              value={selector.channel}
              onChange={createSelectChangeHandler("channel")}
            >
              <MenuItem value="ALL">全部通道</MenuItem>
              <MenuItem value="SH">沪通道</MenuItem>
              <MenuItem value="SZ">深通道</MenuItem>
            </Select>
          </FormControl>
          <FormControl sx={{ flex: 1 }} disabled={disabled}>
            <InputLabel id={`${idPrefix}-connect-research-direction-label`}>研究方向</InputLabel>
            <Select
              labelId={`${idPrefix}-connect-research-direction-label`}
              label="研究方向"
              value={selector.direction ?? ""}
              onChange={createSelectChangeHandler("direction")}
            >
              <MenuItem value="">全部方向</MenuItem>
              <MenuItem value="NORTHBOUND">NORTHBOUND</MenuItem>
              <MenuItem value="SOUTHBOUND">SOUTHBOUND</MenuItem>
            </Select>
          </FormControl>
        </Stack>
      ) : null}
      {selector.kind === "TRADING_EVENT" ? (
        <FormControl fullWidth disabled={disabled}>
          <InputLabel id={`${idPrefix}-event-operation-label`}>事件类型</InputLabel>
          <Select
            labelId={`${idPrefix}-event-operation-label`}
            label="事件类型"
            value={selector.operation}
            onChange={createSelectChangeHandler("operation")}
          >
            <MenuItem value="DRAGON_TIGER">DRAGON_TIGER</MenuItem>
            <MenuItem value="BLOCK_TRADE">BLOCK_TRADE</MenuItem>
          </Select>
        </FormControl>
      ) : null}
      {selector.kind === "MONEY_FLOW" ? (
        <Stack spacing={1.5}>
          <Stack direction="row" spacing={1.5}>
            <TextField
              sx={{ width: 160 }}
              disabled
              label="操作"
              value={selector.operation === "DAILY" ? "日频资金流" : "资金流排行"}
              helperText="由数据集固定"
            />
            {selector.operation === "RANKING" ? (
              <FormControl sx={{ width: 220 }} disabled={disabled}>
                <InputLabel id={idPrefix + "-money-flow-methodology-label"}>方法学</InputLabel>
                <Select
                  labelId={idPrefix + "-money-flow-methodology-label"}
                  label="方法学"
                  value={selector.methodology}
                  onChange={createSelectChangeHandler("methodology")}
                >
                  <MenuItem value="EASTMONEY_ORDER_SIZE">东财按单笔大小</MenuItem>
                  <MenuItem value="THS_TRADE_DIRECTION">同花顺交易方向</MenuItem>
                </Select>
              </FormControl>
            ) : null}
            <FormControl sx={{ width: 180 }} disabled={disabled}>
              <InputLabel id={idPrefix + "-money-flow-scope-label"}>资金流范围</InputLabel>
              <Select
                labelId={idPrefix + "-money-flow-scope-label"}
                label="资金流范围"
                value={selector.scope}
                onChange={createSelectChangeHandler("scope")}
              >
                {(selector.operation === "DAILY"
                  ? [
                      ["EQUITY", "个股"],
                      ["SECTOR", "行业"],
                      ["MARKET", "全市场"],
                    ]
                  : selector.methodology === "EASTMONEY_ORDER_SIZE"
                    ? [
                        ["EQUITY", "个股"],
                        ["SECTOR", "板块"],
                      ]
                    : [
                        ["EQUITY", "个股"],
                        ["INDUSTRY", "行业"],
                        ["CONCEPT", "概念"],
                      ]
                ).map(([scope, label]) => (
                  <MenuItem key={scope} value={scope}>
                    {label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            {selector.operation === "RANKING" ? (
              <FormControl sx={{ width: 160 }} disabled={disabled}>
                <InputLabel id={idPrefix + "-money-flow-window-label"}>窗口</InputLabel>
                <Select
                  labelId={idPrefix + "-money-flow-window-label"}
                  label="窗口"
                  value={selector.window}
                  onChange={createSelectChangeHandler("window")}
                >
                  {(selector.methodology === "EASTMONEY_ORDER_SIZE" && selector.scope === "EQUITY"
                    ? ["TODAY", "DAY_3", "DAY_5", "DAY_10"]
                    : selector.methodology === "EASTMONEY_ORDER_SIZE"
                      ? ["TODAY", "DAY_5", "DAY_10"]
                      : ["INTRADAY", "DAY_3", "DAY_5", "DAY_10", "DAY_20"]
                  ).map((window) => (
                    <MenuItem key={window} value={window}>
                      {window}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            ) : null}
          </Stack>
          {selector.operation === "DAILY" && selector.scope === "EQUITY" ? (
            <Stack direction="row" spacing={1.5}>
              <FormControl sx={{ width: 120 }} disabled={disabled}>
                <InputLabel id={idPrefix + "-money-flow-exchange-label"}>交易所</InputLabel>
                <Select
                  labelId={idPrefix + "-money-flow-exchange-label"}
                  label="交易所"
                  value={selector.exchange}
                  onChange={createSelectChangeHandler("exchange")}
                >
                  <MenuItem value="SSE">SSE</MenuItem>
                  <MenuItem value="SZSE">SZSE</MenuItem>
                  <MenuItem value="BSE">BSE</MenuItem>
                </Select>
              </FormControl>
              <TextField
                required
                fullWidth
                label="证券代码"
                value={selector.symbol}
                onChange={createTextChangeHandler("symbol")}
                disabled={disabled}
                inputProps={{ maxLength: 6 }}
              />
            </Stack>
          ) : null}
          {selector.operation === "DAILY" && selector.scope === "SECTOR" ? (
            <Stack direction="row" spacing={1.5}>
              <TextField sx={{ width: 220 }} disabled label="分类体系" value={selector.scheme} />
              <TextField
                required
                fullWidth
                label="行业代码"
                value={selector.sectorCode}
                onChange={createTextChangeHandler("sectorCode")}
                disabled={disabled}
                inputProps={{ maxLength: 120 }}
              />
            </Stack>
          ) : null}
          {selector.operation === "RANKING" &&
          selector.methodology === "EASTMONEY_ORDER_SIZE" &&
          selector.scope === "SECTOR" ? (
            <FormControl sx={{ width: 220 }} disabled={disabled}>
              <InputLabel id={idPrefix + "-money-flow-sector-type-label"}>板块类型</InputLabel>
              <Select
                labelId={idPrefix + "-money-flow-sector-type-label"}
                label="板块类型"
                value={selector.sectorType}
                onChange={createSelectChangeHandler("sectorType")}
              >
                <MenuItem value="INDUSTRY">INDUSTRY</MenuItem>
                <MenuItem value="CONCEPT">CONCEPT</MenuItem>
                <MenuItem value="REGION">REGION</MenuItem>
              </Select>
            </FormControl>
          ) : null}
        </Stack>
      ) : null}
      {selector.kind === "INDEX" ? (
        <Stack direction="row" spacing={1.5}>
          <TextField sx={{ width: 120 }} disabled label="管理方" value={selector.administrator} />
          <TextField fullWidth label="能力" value={selector.capability} disabled />
          {selector.indexCode !== null ? (
            <TextField
              required
              fullWidth
              label="指数代码"
              value={selector.indexCode}
              onChange={createTextChangeHandler("indexCode")}
              disabled={disabled}
              inputProps={{ maxLength: 8 }}
            />
          ) : null}
        </Stack>
      ) : null}
    </Stack>
  );
}
