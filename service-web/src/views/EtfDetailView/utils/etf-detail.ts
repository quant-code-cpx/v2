import type { Candle } from "../../../types/candle";
import type {
  EtfDailyBarValues,
  EtfExchange,
  EtfNavValues,
  EtfTradingStateValues,
} from "../../../types/etf";

/** ETF 详情路由中经过白名单校验的产品身份。 */
export interface EtfRouteIdentity {
  exchange: EtfExchange;
  symbol: string;
}

/** ECharts 价格与 NAV 原值比较的一行数据。 */
export interface EtfNavPricePoint {
  date: string;
  close: number | null;
  nav: number | null;
}

/** 校验交易所和六位代码，不通过代码内容推断交易所或基金类型。 */
export function parseEtfRouteIdentity(
  exchange: string | undefined,
  symbol: string | undefined,
): EtfRouteIdentity | null {
  if ((exchange !== "SSE" && exchange !== "SZSE") || !/^\d{6}$/u.test(symbol ?? "")) {
    return null;
  }

  return { exchange, symbol: symbol ?? "" };
}

/** 将严格校验后的日线十进制字段转换为 KLineChart 数值输入。 */
export function toEtfCandles(values: readonly EtfDailyBarValues[]): Candle[] {
  return values.map(
    /** 每根日线只进行图表引擎需要的数值转换，不改写来源口径。 */
    (bar) => ({
      timestamp: new Date(`${bar.tradeDate}T00:00:00+08:00`).getTime(),
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
      volume: Number(bar.volume),
      turnover: Number(bar.amount),
    }),
  );
}

/** 按业务日期返回最后一个来源值，不对价格或 NAV 做插值。 */
export function latestByDate<T>(
  values: readonly T[],
  readDate: (value: T) => string,
): T | undefined {
  let latest: T | undefined;
  let latestDate = "";

  for (const value of values) {
    const valueDate = readDate(value);
    if (valueDate >= latestDate) {
      latest = value;
      latestDate = valueDate;
    }
  }

  return latest;
}

/** 以日期并集对齐收盘价和单位 NAV 原值，不计算折溢价或填补缺失日。 */
export function createEtfNavPricePoints(
  bars: readonly EtfDailyBarValues[],
  navs: readonly EtfNavValues[],
): EtfNavPricePoint[] {
  const byDate = new Map<string, EtfNavPricePoint>();

  for (const bar of bars) {
    byDate.set(bar.tradeDate, {
      date: bar.tradeDate,
      close: Number(bar.close),
      nav: byDate.get(bar.tradeDate)?.nav ?? null,
    });
  }
  for (const nav of navs) {
    const current = byDate.get(nav.navDate);
    byDate.set(nav.navDate, {
      date: nav.navDate,
      close: current?.close ?? null,
      nav: Number(nav.nav),
    });
  }

  return Array.from(byDate.values()).toSorted(
    /** 图表 x 轴严格按 ISO 日期升序。 */
    (left, right) => left.date.localeCompare(right.date),
  );
}

/** 按独立状态维度选择查询窗口内最近报告的来源记录。 */
export function latestEtfStates(
  values: readonly EtfTradingStateValues[],
): ReadonlyMap<EtfTradingStateValues["stateDimension"], EtfTradingStateValues> {
  const latest = new Map<EtfTradingStateValues["stateDimension"], EtfTradingStateValues>();

  for (const value of values) {
    const current = latest.get(value.stateDimension);
    if (current === undefined || value.effectiveFrom >= current.effectiveFrom) {
      latest.set(value.stateDimension, value);
    }
  }

  return latest;
}
