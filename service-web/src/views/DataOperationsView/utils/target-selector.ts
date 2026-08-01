import type {
  IndexTargetSelector,
  MarginTargetSelector,
  MoneyFlowTargetSelector,
  RuntimeTargetSelector,
  StockConnectResearchTargetSelector,
  TargetSelector,
  TargetSelectorKind,
} from "../../../types/data-operations";

/** ETF canonical dataset 对应的唯一同步操作。 */
type EtfOperation = "MASTER" | "STATUS" | "BARS" | "NAV";

/** 显式绑定 ETF 数据集与操作，禁止由页面选择或代码前缀推断实际执行能力。 */
const ETF_OPERATION_BY_DATASET_CODE: Readonly<Record<string, EtfOperation>> = {
  "fund.etf.profile.reported": "MASTER",
  "fund.etf.trading_state.reported": "STATUS",
  "fund.etf.bar.1d.reported": "BARS",
  "fund.etf.nav.1d.reported": "NAV",
};

/** 显式绑定指数数据集与唯一管理方、能力和代码范围，禁止由页面自由组合。 */
const INDEX_TARGET_BY_DATASET_CODE: Readonly<Record<string, IndexTargetSelector>> = {
  "index.csi.catalog.snapshot": {
    kind: "INDEX",
    administrator: "CSI",
    capability: "index.catalog.snapshot",
    indexCode: null,
  },
  "index.csi.constituent.snapshot": {
    kind: "INDEX",
    administrator: "CSI",
    capability: "index.constituent.snapshot",
    indexCode: "",
  },
  "index.csi.weight.snapshot": {
    kind: "INDEX",
    administrator: "CSI",
    capability: "index.weight.snapshot",
    indexCode: "",
  },
  "index.cni.catalog.snapshot": {
    kind: "INDEX",
    administrator: "CNI",
    capability: "index.catalog.snapshot",
    indexCode: null,
  },
  "index.cni.constituent.snapshot": {
    kind: "INDEX",
    administrator: "CNI",
    capability: "index.constituent.snapshot",
    indexCode: "",
  },
  "index.cni.weight.snapshot": {
    kind: "INDEX",
    administrator: "CNI",
    capability: "index.weight.snapshot",
    indexCode: "",
  },
};

/** 显式绑定资金流数据集与唯一操作及其可立即编辑的有效初始范围。 */
const MONEY_FLOW_TARGET_BY_DATASET_CODE: Readonly<Record<string, MoneyFlowTargetSelector>> = {
  "money_flow.daily": {
    kind: "MONEY_FLOW",
    operation: "DAILY",
    scope: "MARKET",
  },
  "money_flow.ranking": {
    kind: "MONEY_FLOW",
    operation: "RANKING",
    methodology: "EASTMONEY_ORDER_SIZE",
    scope: "EQUITY",
    window: "TODAY",
  },
};

/** 显式绑定两融数据集、唯一 operation 与可立即执行的市场默认值。 */
const MARGIN_TARGET_BY_DATASET_CODE: Readonly<Record<string, MarginTargetSelector>> = {
  "market.margin.market.1d.reported": {
    kind: "MARGIN",
    operation: "MARKET",
    venue: "SSE",
    security: null,
  },
  "market.margin.security.1d.reported": {
    kind: "MARGIN",
    operation: "SECURITY",
    venue: "SSE",
    security: null,
  },
  "market.margin.eligibility.reported": {
    kind: "MARGIN",
    operation: "ELIGIBILITY",
    venue: "BSE",
    security: null,
  },
};

/** 显式绑定唯一港通市场统计 `research` 数据集，未知数据集不构造 `selector`。 */
const STOCK_CONNECT_RESEARCH_TARGET_BY_DATASET_CODE: Readonly<
  Record<string, StockConnectResearchTargetSelector>
> = {
  "market.stock_connect.market_stat.research": {
    kind: "STOCK_CONNECT_RESEARCH",
    operation: "MARKET_STAT",
    channel: "ALL",
    direction: null,
  },
};

/** 返回 ETF canonical dataset 的固定操作；未知代码 fail-closed。 */
export function etfOperationForDataset(datasetCode: string): EtfOperation | undefined {
  return ETF_OPERATION_BY_DATASET_CODE[datasetCode];
}

/** 返回指数 canonical dataset 的冻结 selector 草稿；未知代码 fail-closed。 */
export function indexTargetForDataset(datasetCode: string): IndexTargetSelector | undefined {
  const selector = INDEX_TARGET_BY_DATASET_CODE[datasetCode];
  return selector === undefined ? undefined : { ...selector };
}

/** 返回资金流 canonical dataset 的冻结操作草稿；未知代码 fail-closed。 */
export function moneyFlowTargetForDataset(
  datasetCode: string,
): MoneyFlowTargetSelector | undefined {
  const selector = MONEY_FLOW_TARGET_BY_DATASET_CODE[datasetCode];
  return selector === undefined ? undefined : { ...selector };
}

/** 返回两融 canonical dataset 的冻结 operation 草稿；未知数据集 fail-closed。 */
export function marginTargetForDataset(datasetCode: string): MarginTargetSelector | undefined {
  const selector = MARGIN_TARGET_BY_DATASET_CODE[datasetCode];
  return selector === undefined ? undefined : { ...selector };
}

/** 返回港通市场统计 `research` 的受限默认范围；未知数据集拒绝构造。 */
export function stockConnectResearchTargetForDataset(
  datasetCode: string,
): StockConnectResearchTargetSelector | undefined {
  const selector = STOCK_CONNECT_RESEARCH_TARGET_BY_DATASET_CODE[datasetCode];
  return selector === undefined ? undefined : { ...selector };
}

/** 将合同选择器类别转换为面向运维人员的稳定中文标签。 */
export function targetSelectorKindLabel(kind: TargetSelectorKind): string {
  const labels: Record<TargetSelectorKind, string> = {
    GLOBAL: "全数据集",
    INSTRUMENT: "单证券",
    SECTOR: "单行业",
    SCHEME: "分类体系",
    EXCHANGE: "证券交易所",
    CONTRACT: "期货合约",
    ETF: "ETF",
    MARGIN: "两融",
    STOCK_CONNECT: "沪深港通",
    STOCK_CONNECT_RESEARCH: "港通市场统计（研究）",
    TRADING_EVENT: "交易事件",
    INDEX: "指数",
    MONEY_FLOW: "资金流",
  };
  return labels[kind];
}

/** 从合同允许的 selector kind 与数据集创建不带任意参数的最小受控草稿。 */
export function createTargetSelector(
  kind: TargetSelectorKind,
  datasetCode: string,
): TargetSelector | undefined {
  switch (kind) {
    case "GLOBAL":
      return { kind };
    case "INSTRUMENT":
      return { kind, exchange: "SSE", symbol: "" };
    case "SECTOR":
      return { kind, scheme: "", sectorCode: "" };
    case "SCHEME":
      return { kind, scheme: "" };
    case "EXCHANGE":
      return { kind, exchange: "SSE" };
    case "CONTRACT":
      return { kind, venue: "CFFEX", contract: "" };
    case "ETF": {
      const operation = etfOperationForDataset(datasetCode);
      if (operation === undefined) return undefined;
      return operation === "MASTER"
        ? { kind, operation, venue: null, scope: "ALL_VENUES", etf: null }
        : { kind, operation, venue: null, etf: "" };
    }
    case "MARGIN":
      return marginTargetForDataset(datasetCode);
    case "STOCK_CONNECT":
      return { kind, operation: "MARKET", channel: "ALL", direction: null };
    case "STOCK_CONNECT_RESEARCH":
      return stockConnectResearchTargetForDataset(datasetCode);
    case "TRADING_EVENT":
      return { kind, operation: "DRAGON_TIGER" };
    case "INDEX":
      return indexTargetForDataset(datasetCode);
    case "MONEY_FLOW":
      return moneyFlowTargetForDataset(datasetCode);
  }
}

/** 优先选择 capability 明确允许的 `GLOBAL`，否则取服务端返回的首个选择器类别。 */
export function createDefaultTargetSelector(
  selectorKinds: TargetSelectorKind[],
  datasetCode: string,
): TargetSelector | undefined {
  const kind = selectorKinds.includes("GLOBAL") ? "GLOBAL" : selectorKinds[0];
  return kind === undefined ? undefined : createTargetSelector(kind, datasetCode);
}

/** 校验不含空白的有限长度文本，防止前端把不完整目标送进预检。 */
function hasBoundedText(value: string, maximumLength: number): boolean {
  return value.trim().length > 0 && value.length <= maximumLength;
}

/** 校验单个证券代码是否符合公开合同的受限格式。 */
function isInstrumentSymbol(value: string): boolean {
  return /^[0-9A-Z.-]{1,32}$/.test(value);
}

/** 校验期货合约代码是否符合公开合同的受限格式。 */
function isContractCode(value: string): boolean {
  return /^[0-9A-Z._-]+$/.test(value) && value.length <= 64;
}

/** 校验用户显式输入的 ETF canonical identity 格式，不据此前缀推断基金分类。 */
function isEtfCode(value: string): boolean {
  return /^(SSE|SZSE)\.[0-9]{6}$/.test(value);
}

/** 校验实测的 CSI、CNI 指数标识，代码只能是六码至八码大写字母数字。 */
function isIndexCode(value: string): boolean {
  return /^[A-Z0-9]{6,8}$/.test(value);
}

/** 校验个股日频资金流要求的六码纯数字证券代码。 */
function isMoneyFlowEquitySymbol(value: string): boolean {
  return /^[0-9]{6}$/.test(value);
}

/** 校验由客户端受控表单构造的 selector 是否具备进入服务端预检的基础形状。 */
export function isTargetSelectorStructurallyReady(selector: TargetSelector): boolean {
  switch (selector.kind) {
    case "GLOBAL":
    case "EXCHANGE":
    case "STOCK_CONNECT":
    case "TRADING_EVENT":
      return true;
    case "STOCK_CONNECT_RESEARCH":
      return (
        selector.operation === "MARKET_STAT" &&
        ["ALL", "SH", "SZ"].includes(selector.channel) &&
        [null, "NORTHBOUND", "SOUTHBOUND"].includes(selector.direction)
      );
    case "INSTRUMENT":
      return isInstrumentSymbol(selector.symbol);
    case "SECTOR":
      return hasBoundedText(selector.scheme, 64) && hasBoundedText(selector.sectorCode, 120);
    case "SCHEME":
      return hasBoundedText(selector.scheme, 64);
    case "CONTRACT":
      return isContractCode(selector.contract);
    case "ETF":
      if (selector.operation === "MASTER") {
        return "scope" in selector
          ? selector.scope === "ALL_VENUES" && selector.venue === null && selector.etf === null
          : selector.etf === null;
      }
      if ("scope" in selector) {
        return selector.etf === null && selector.profileDataVersions === null;
      }
      return (
        isEtfCode(selector.etf) &&
        (selector.venue === null || selector.etf.startsWith(`${selector.venue}.`))
      );
    case "MARGIN":
      if (selector.security !== null) return false;
      return selector.operation === "ELIGIBILITY"
        ? selector.venue === "SZSE" || selector.venue === "BSE"
        : selector.venue === "SSE" || selector.venue === "SZSE";
    case "INDEX":
      return selector.indexCode === null
        ? selector.capability === "index.catalog.snapshot"
        : (selector.capability === "index.constituent.snapshot" ||
            selector.capability === "index.weight.snapshot") &&
            isIndexCode(selector.indexCode);
    case "MONEY_FLOW":
      if (selector.operation === "DAILY") {
        if (selector.scope === "EQUITY") return isMoneyFlowEquitySymbol(selector.symbol);
        if (selector.scope === "SECTOR")
          return (
            selector.scheme === "eastmoney.industry" && hasBoundedText(selector.sectorCode, 120)
          );
        return selector.scope === "MARKET";
      }
      if (selector.methodology === "EASTMONEY_ORDER_SIZE") {
        return selector.scope === "EQUITY"
          ? ["TODAY", "DAY_3", "DAY_5", "DAY_10"].includes(selector.window)
          : selector.scope === "SECTOR" &&
              ["INDUSTRY", "CONCEPT", "REGION"].includes(selector.sectorType) &&
              ["TODAY", "DAY_5", "DAY_10"].includes(selector.window);
      }
      return (
        ["EQUITY", "INDUSTRY", "CONCEPT"].includes(selector.scope) &&
        ["INTRADAY", "DAY_3", "DAY_5", "DAY_10", "DAY_20"].includes(selector.window)
      );
  }
}

/** 生成不包含内部参数的选择器摘要，用于计划和预检结果展示。 */
export function targetSelectorSummary(selector: RuntimeTargetSelector): string {
  switch (selector.kind) {
    case "GLOBAL":
      return "全数据集";
    case "INSTRUMENT":
      return `${selector.exchange}.${selector.symbol || "未指定"}`;
    case "SECTOR":
      return `${selector.scheme || "未指定体系"}.${selector.sectorCode || "未指定行业"}`;
    case "SCHEME":
      return selector.scheme || "未指定分类体系";
    case "EXCHANGE":
      return selector.exchange;
    case "CONTRACT":
      return `${selector.venue}.${selector.contract || "未指定合约"}`;
    case "ETF":
      if (selector.operation === "MASTER")
        return "scope" in selector && selector.scope === "ALL_VENUES"
          ? `${selector.operation}.沪深全市场`
          : `${selector.operation}.${selector.venue}`;
      return "scope" in selector && selector.scope === "ALL_ETFS"
        ? `${selector.operation}.全部已发布 ETF`
        : `${selector.operation}.${selector.etf || "未指定 ETF"}`;
    case "MARGIN":
      return `${selector.operation}.${selector.venue}`;
    case "STOCK_CONNECT":
      return `完整互联互通数据包 · ${
        selector.channel === "ALL" ? "全部通道" : selector.channel === "SH" ? "沪通道" : "深通道"
      } · ${
        selector.direction === null
          ? "全部方向"
          : selector.direction === "NORTHBOUND"
            ? "北向"
            : "南向"
      }`;
    case "STOCK_CONNECT_RESEARCH":
      return `港通市场统计（research） · ${
        selector.channel === "ALL" ? "全部通道" : selector.channel === "SH" ? "沪通道" : "深通道"
      } · ${
        selector.direction === null
          ? "全部方向"
          : selector.direction === "NORTHBOUND"
            ? "北向"
            : "南向"
      }`;
    case "TRADING_EVENT":
      return selector.operation;
    case "MONEY_FLOW":
      if (selector.operation === "DAILY") {
        if (selector.scope === "MARKET") return "DAILY.MARKET";
        if (selector.scope === "EQUITY")
          return "DAILY.EQUITY." + selector.exchange + "." + (selector.symbol || "未指定证券");
        return "DAILY.SECTOR." + selector.scheme + "." + (selector.sectorCode || "未指定行业");
      }
      return (
        selector.operation +
        "." +
        selector.methodology +
        "." +
        selector.scope +
        (selector.methodology === "EASTMONEY_ORDER_SIZE" && selector.scope === "SECTOR"
          ? "." + selector.sectorType
          : "") +
        "." +
        selector.window
      );
    case "INDEX":
      return selector.indexCode === null
        ? `${selector.administrator}.${selector.capability}`
        : `${selector.administrator}.${selector.capability}.${selector.indexCode || "未指定指数"}`;
  }
}
