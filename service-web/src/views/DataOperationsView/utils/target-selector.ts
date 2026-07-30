import type {
  RuntimeTargetSelector,
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

/** 返回 ETF canonical dataset 的固定操作；未知代码 fail-closed。 */
export function etfOperationForDataset(datasetCode: string): EtfOperation | undefined {
  return ETF_OPERATION_BY_DATASET_CODE[datasetCode];
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
    TRADING_EVENT: "交易事件",
    INDEX: "指数",
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
      return { kind, operation: "MARKET", venue: "SSE", security: null };
    case "STOCK_CONNECT":
      return { kind, operation: "MARKET", channel: "ALL", direction: null };
    case "TRADING_EVENT":
      return { kind, operation: "DRAGON_TIGER" };
    case "INDEX":
      return { kind, administrator: "CSI", capability: "", indexCode: "" };
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

/** 校验由客户端受控表单构造的 selector 是否具备进入服务端预检的基础形状。 */
export function isTargetSelectorStructurallyReady(selector: TargetSelector): boolean {
  switch (selector.kind) {
    case "GLOBAL":
    case "EXCHANGE":
    case "STOCK_CONNECT":
    case "TRADING_EVENT":
      return true;
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
      return selector.security === null || isInstrumentSymbol(selector.security.symbol);
    case "INDEX":
      return hasBoundedText(selector.capability, 120) && hasBoundedText(selector.indexCode, 64);
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
      return `${selector.operation}.${selector.venue}${
        selector.security === null ? "" : `.${selector.security.symbol}`
      }`;
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
    case "TRADING_EVENT":
      return selector.operation;
    case "INDEX":
      return `${selector.administrator}.${selector.capability || "未指定能力"}.${
        selector.indexCode || "未指定指数"
      }`;
  }
}
