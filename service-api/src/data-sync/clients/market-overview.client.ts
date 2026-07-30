import { HttpStatus, Injectable } from '@nestjs/common';
import type { ZodType } from 'zod';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  marketCalendarPageSchema,
  marketEquityMoneyFlowRankingPageSchema,
  marketEquityRankingPageSchema,
  marketIndexBarPageSchema,
  marketOverviewSchema,
  marketSectorMoneyFlowRankingPageSchema,
  marketSectorStrengthPageSchema,
  swIndustryBarPageSchema,
  swIndustryConstituentPageSchema,
  swIndustryValuationSchema,
  type MarketCalendarPage,
  type MarketEquityMoneyFlowRankingPage,
  type MarketEquityRankingPage,
  type MarketIndexBarPage,
  type MarketOverview,
  type MarketSectorMoneyFlowRankingPage,
  type MarketSectorStrengthPage,
  type SwIndustryBarPage,
  type SwIndustryConstituentPage,
  type SwIndustryValuation,
} from '../contracts/market-overview.contract.js';

type FetchLike = typeof fetch;

/** 描述所有市场读取响应共有的不可变 publication 标识。 */
type VersionedMarketBody = { dataVersion: string };

/** 描述内部资源响应必须与公开请求绑定的身份和查询维度。 */
type MarketResponseExpectation =
  | { kind: 'overview'; tradeDate?: string | undefined }
  | {
      kind: 'index-bars';
      indexId: string;
      period: '1d';
      start: string;
      end: string;
    }
  | {
      kind: 'equity-ranking';
      tradeDate?: string | undefined;
      metric: string;
      order: string;
    }
  | {
      kind: 'equity-money-flow';
      tradeDate?: string | undefined;
      direction: string;
    }
  | {
      kind: 'calendar';
      venues: readonly string[];
      start: string;
      end: string;
    }
  | {
      kind: 'sector-strength';
      tradeDate?: string | undefined;
      scheme: string;
      window: number;
      order: string;
    }
  | {
      kind: 'sector-money-flow';
      tradeDate?: string | undefined;
      scheme: string;
      order: string;
    }
  | {
      kind: 'sw-bars';
      code: string;
      period: '1d' | '1w' | '1mo';
      start: string;
      end: string;
    }
  | {
      kind: 'sw-constituents';
      code: string;
      snapshotDate?: string | undefined;
    }
  | { kind: 'sw-valuation'; code: string; tradeDate?: string | undefined };

/** 描述内部 304 与公开 POST 204 映射所需的条件读取元数据。 */
export type MarketConditionalRead<T extends VersionedMarketBody> =
  | { status: 304; etag: string; dataVersion: string }
  | { status: 200; etag: string; dataVersion: string; body: T };

/** 描述市场内部读取断路器的三个稳定状态。 */
type CircuitState = 'closed' | 'open' | 'half-open';

const FAILURE_WINDOW_MS = 30_000;
const OPEN_DURATION_MS = 15_000;
const FAILURE_THRESHOLD = 5;
const TOTAL_REQUEST_BUDGET_MS = 2_000;
const SINGLE_ATTEMPT_TIMEOUT_MS = 1_500;

/** 通过版本化内部 GET 读取已发布市场事实，不访问同步库或 Tushare SDK。 */
@Injectable()
export class MarketOverviewClient {
  private circuitState: CircuitState = 'closed';
  private circuitOpenedAt: number | undefined;
  private failureTimes: number[] = [];

  /** 使用集中配置和可替换 fetch 构造市场读取防腐边界。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 读取 latest 或精确交易日的单个原子首页完整包。 */
  public getOverview(input: {
    asOf?: string | undefined;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketOverview>> {
    const snapshot = input.asOf ?? 'latest';
    return this.request(
      `/internal/v1/market/overview-bundles/${encodeURIComponent(snapshot)}`,
      input.ifNoneMatch,
      input.requestId,
      marketOverviewSchema,
      256 * 1_024,
      { kind: 'overview', tradeDate: input.asOf },
    );
  }

  /** 读取固定指数身份的来源日 K 线，不从指数成分观察推导点位。 */
  public listIndexBars(input: {
    indexId: string;
    period: '1d';
    start: string;
    end: string;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketIndexBarPage>> {
    const parameters = new URLSearchParams({
      period: input.period,
      start: input.start,
      end: input.end,
      limit: String(input.limit),
    });
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/indices/${encodeURIComponent(input.indexId)}/bars?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      marketIndexBarPageSchema,
      2 * 1_024 * 1_024,
      {
        kind: 'index-bars',
        indexId: input.indexId,
        period: input.period,
        start: input.start,
        end: input.end,
      },
    );
  }

  /** 读取冻结全市场横截面派生的证券排行，绝不拼接单股 latest。 */
  public listEquityRankings(input: {
    asOf?: string | undefined;
    metric: 'changePercent' | 'amountCny' | 'turnoverPercent';
    order: 'asc' | 'desc';
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketEquityRankingPage>> {
    const parameters = new URLSearchParams({
      metric: input.metric,
      order: input.order,
      limit: String(input.limit),
    });
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/equity-rankings?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      marketEquityRankingPageSchema,
      512 * 1_024,
      {
        kind: 'equity-ranking',
        tradeDate: input.asOf,
        metric: input.metric,
        order: input.order,
      },
    );
  }

  /** 读取 Tushare 订单规模方法学下的一侧证券资金流排行。 */
  public listEquityMoneyFlowRankings(input: {
    asOf?: string | undefined;
    direction: 'inflow' | 'outflow';
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketEquityMoneyFlowRankingPage>> {
    const parameters = new URLSearchParams({
      direction: input.direction,
      limit: String(input.limit),
    });
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/money-flow/equity-rankings?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      marketEquityMoneyFlowRankingPageSchema,
      512 * 1_024,
      {
        kind: 'equity-money-flow',
        tradeDate: input.asOf,
        direction: input.direction,
      },
    );
  }

  /** 读取沪深交易日历和会话日程，当前状态仍由服务端按 Asia/Shanghai 解释。 */
  public queryCalendar(input: {
    venues: readonly ('SSE' | 'SZSE')[];
    start: string;
    end: string;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketCalendarPage>> {
    const parameters = new URLSearchParams({
      venues: input.venues.join(','),
      start: input.start,
      end: input.end,
    });
    return this.request(
      `/internal/v1/market/calendar?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      marketCalendarPageSchema,
      2 * 1_024 * 1_024,
      {
        kind: 'calendar',
        venues: input.venues,
        start: input.start,
        end: input.end,
      },
    );
  }

  /** 读取独立发布的板块强弱序列，不在请求线程跨日扫描 EOD 表。 */
  public listSectorStrength(input: {
    scheme: 'eastmoney.industry' | 'eastmoney.concept';
    asOf?: string | undefined;
    window: 1 | 5 | 20;
    order: 'asc' | 'desc';
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketSectorStrengthPage>> {
    const parameters = new URLSearchParams({
      scheme: input.scheme,
      window: String(input.window),
      order: input.order,
      limit: String(input.limit),
    });
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/sectors/strength?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      marketSectorStrengthPageSchema,
      1_024 * 1_024,
      {
        kind: 'sector-strength',
        tradeDate: input.asOf,
        scheme: input.scheme,
        window: input.window,
        order: input.order,
      },
    );
  }

  /** 读取东财来源板块资金流排行，不把价格强弱描述成资金方向。 */
  public listSectorMoneyFlowRankings(input: {
    scheme: 'eastmoney.industry' | 'eastmoney.concept';
    asOf?: string | undefined;
    order: 'asc' | 'desc';
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<MarketSectorMoneyFlowRankingPage>> {
    const parameters = new URLSearchParams({
      scheme: input.scheme,
      order: input.order,
      limit: String(input.limit),
    });
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/sectors/money-flow-rankings?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      marketSectorMoneyFlowRankingPageSchema,
      1_024 * 1_024,
      {
        kind: 'sector-money-flow',
        tradeDate: input.asOf,
        scheme: input.scheme,
        order: input.order,
      },
    );
  }

  /** 读取申万行业来源日线，周月周期不会由 API 临时聚合。 */
  public listSwIndustryBars(input: {
    code: string;
    period: '1d' | '1w' | '1mo';
    start: string;
    end: string;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<SwIndustryBarPage>> {
    const parameters = new URLSearchParams({
      period: input.period,
      start: input.start,
      end: input.end,
      limit: String(input.limit),
    });
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/industries/sw/${encodeURIComponent(input.code)}/bars?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      swIndustryBarPageSchema,
      2 * 1_024 * 1_024,
      {
        kind: 'sw-bars',
        code: input.code,
        period: input.period,
        start: input.start,
        end: input.end,
      },
    );
  }

  /** 读取申万正式成分 publication，不把东财同名行业或观察区间作为替代。 */
  public listSwIndustryConstituents(input: {
    code: string;
    asOf?: string | undefined;
    cursor?: string | undefined;
    limit: number;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<SwIndustryConstituentPage>> {
    const parameters = new URLSearchParams({ limit: String(input.limit) });
    setOptional(parameters, 'asOf', input.asOf);
    setOptional(parameters, 'cursor', input.cursor);
    return this.request(
      `/internal/v1/market/industries/sw/${encodeURIComponent(input.code)}/constituents?${parameters.toString()}`,
      input.ifNoneMatch,
      input.requestId,
      swIndustryConstituentPageSchema,
      1_024 * 1_024,
      { kind: 'sw-constituents', code: input.code, snapshotDate: input.asOf },
    );
  }

  /** 读取单个申万节点逐字段可解释的估值，不扫描整层分页。 */
  public getSwIndustryValuation(input: {
    code: string;
    asOf?: string | undefined;
    ifNoneMatch?: string | undefined;
    requestId: string;
  }): Promise<MarketConditionalRead<SwIndustryValuation>> {
    const parameters = new URLSearchParams();
    setOptional(parameters, 'asOf', input.asOf);
    const suffix = parameters.size === 0 ? '' : `?${parameters.toString()}`;
    return this.request(
      `/internal/v1/market/industries/sw/${encodeURIComponent(input.code)}/valuation${suffix}`,
      input.ifNoneMatch,
      input.requestId,
      swIndustryValuationSchema,
      256 * 1_024,
      { kind: 'sw-valuation', code: input.code, tradeDate: input.asOf },
    );
  }

  /** 发起有身份、条件缓存、响应上限、断路和严格 schema 校验的内部 GET。 */
  private async request<T extends VersionedMarketBody>(
    requestPath: string,
    ifNoneMatch: string | undefined,
    requestId: string,
    schema: ZodType<T>,
    maximumBytes: number,
    expectation: MarketResponseExpectation,
  ): Promise<MarketConditionalRead<T>> {
    if (!validRequestId(requestId)) throw dependencyUnavailable();
    this.assertCircuitAllowsRequest();
    const url = new URL(requestPath, this.config.dataSyncInternalBaseUrl);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      Authorization: `Bearer ${this.config.dataSyncInternalBearerToken}`,
      'X-Request-Id': requestId,
    };
    if (ifNoneMatch !== undefined) headers['If-None-Match'] = ifNoneMatch;

    let response: Response;
    try {
      response = await this.fetchWithSingleRetry(url, headers);
    } catch {
      this.recordDependencyFailure();
      throw dependencyUnavailable();
    }

    const etag = response.headers.get('etag');
    const dataVersion = response.headers.get('x-data-version');
    if (response.headers.get('x-request-id') !== requestId) {
      await response.body?.cancel();
      this.recordDependencyFailure();
      throw dependencyUnavailable();
    }
    if (response.status === 304) {
      if (!validEtag(etag) || !validDataVersion(dataVersion)) {
        this.recordDependencyFailure();
        throw dependencyUnavailable();
      }
      this.recordDependencySuccess();
      return { status: 304, etag, dataVersion };
    }

    if (!response.ok) {
      if (isDependencyFailureStatus(response.status)) {
        this.recordDependencyFailure();
      } else {
        this.recordDependencySuccess();
      }
      throw await upstreamProblem(response);
    }

    try {
      const body = schema.parse(await readJsonObject(response, maximumBytes));
      if (
        !validEtag(etag) ||
        !validDataVersion(dataVersion) ||
        body.dataVersion !== dataVersion ||
        !matchesMarketResponseExpectation(body, expectation)
      ) {
        throw new Error('publication headers do not match body');
      }
      this.recordDependencySuccess();
      return { status: 200, etag, dataVersion, body };
    } catch {
      this.recordDependencyFailure();
      throw dependencyUnavailable();
    }
  }

  /** 对幂等内部 GET 的网络错误与 502/503/504 最多安全重试一次。 */
  private async fetchWithSingleRetry(url: URL, headers: Record<string, string>): Promise<Response> {
    const deadline = Date.now() + TOTAL_REQUEST_BUDGET_MS;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const remainingBudget = deadline - Date.now();
        if (remainingBudget <= 0) throw new Error('market request budget exhausted');
        const response = await this.fetcher(url, {
          method: 'GET',
          headers,
          // 市场首页公开总预算为 2 秒，单次下游调用即使全局配置更宽也最多占用 1.5 秒。
          signal: AbortSignal.timeout(
            Math.max(
              1,
              Math.min(
                this.config.dataSyncInternalRequestTimeoutMs,
                SINGLE_ATTEMPT_TIMEOUT_MS,
                remainingBudget,
              ),
            ),
          ),
        });
        if (attempt === 0 && [502, 503, 504].includes(response.status)) {
          // 重试前主动释放首个失败响应体，避免连接池长期占用未消费流。
          await response.body?.cancel();
          continue;
        }
        return response;
      } catch {
        if (attempt === 1) throw new Error('market dependency request failed');
      }
    }
    throw new Error('market dependency request failed');
  }

  /** 在断路窗口内快速失败，并只允许一个半开探针请求触碰下游。 */
  private assertCircuitAllowsRequest(): void {
    const now = Date.now();
    if (this.circuitState === 'closed') return;
    if (
      this.circuitState === 'open' &&
      this.circuitOpenedAt !== undefined &&
      now - this.circuitOpenedAt >= OPEN_DURATION_MS
    ) {
      this.circuitState = 'half-open';
      return;
    }
    throw dependencyUnavailable(15);
  }

  /** 在一次完整逻辑请求成功后关闭断路器并清空旧失败。 */
  private recordDependencySuccess(): void {
    this.circuitState = 'closed';
    this.circuitOpenedAt = undefined;
    this.failureTimes = [];
  }

  /** 记录一次完整逻辑请求失败，达到窗口阈值时打开断路器。 */
  private recordDependencyFailure(): void {
    const now = Date.now();
    if (this.circuitState === 'half-open') {
      this.circuitState = 'open';
      this.circuitOpenedAt = now;
      return;
    }
    this.failureTimes = this.failureTimes.filter(
      // 只保留当前 30 秒窗口中的逻辑请求失败。
      (occurredAt) => now - occurredAt <= FAILURE_WINDOW_MS,
    );
    this.failureTimes.push(now);
    if (this.failureTimes.length >= FAILURE_THRESHOLD) {
      this.circuitState = 'open';
      this.circuitOpenedAt = now;
    }
  }
}

/** 添加一个非空可选查询参数，避免序列化出字符串 `undefined`。 */
function setOptional(parameters: URLSearchParams, key: string, value: string | undefined): void {
  if (value !== undefined) parameters.set(key, value);
}

/** 校验下游返回不含换行的强 ETag。 */
function validEtag(value: string | null): value is string {
  return value !== null && /^"[^"\r\n]{1,252}"$/.test(value);
}

/** 校验下游 publication 版本采用 UUID。 */
function validDataVersion(value: string | null): value is string {
  return (
    value !== null &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  );
}

/** 校验关联标识采用 UUID 或受限稳定字符集，禁止换行和空白进入服务间请求头。 */
function validRequestId(value: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(value);
}

/** 校验严格 schema 通过后的响应仍与请求资源、周期和精确日期一致。 */
function matchesMarketResponseExpectation(
  body: VersionedMarketBody,
  expectation: MarketResponseExpectation,
): boolean {
  if (expectation.kind === 'overview') {
    const value = body as MarketOverview;
    return expectation.tradeDate === undefined || value.tradeDate === expectation.tradeDate;
  }
  if (expectation.kind === 'index-bars') {
    const value = body as MarketIndexBarPage;
    if (value.index.indexId !== expectation.indexId || value.period !== expectation.period) {
      return false;
    }
    for (const item of value.items) {
      if (!dateInRange(item.tradeDate, expectation.start, expectation.end)) return false;
    }
    return true;
  }
  if (expectation.kind === 'equity-ranking') {
    const value = body as MarketEquityRankingPage;
    return (
      (expectation.tradeDate === undefined || value.tradeDate === expectation.tradeDate) &&
      value.metric === expectation.metric &&
      value.order === expectation.order
    );
  }
  if (expectation.kind === 'equity-money-flow') {
    const value = body as MarketEquityMoneyFlowRankingPage;
    return (
      (expectation.tradeDate === undefined || value.tradeDate === expectation.tradeDate) &&
      value.direction === expectation.direction
    );
  }
  if (expectation.kind === 'calendar') {
    const value = body as MarketCalendarPage;
    const seenVenues = new Set<string>();
    for (const item of value.items) {
      if (
        !expectation.venues.includes(item.venue) ||
        !dateInRange(item.tradeDate, expectation.start, expectation.end)
      ) {
        return false;
      }
      seenVenues.add(item.venue);
    }
    for (const venue of expectation.venues) {
      if (!seenVenues.has(venue)) return false;
    }
    return true;
  }
  if (expectation.kind === 'sector-strength') {
    const value = body as MarketSectorStrengthPage;
    return (
      (expectation.tradeDate === undefined || value.tradeDate === expectation.tradeDate) &&
      value.scheme === expectation.scheme &&
      value.window === expectation.window &&
      value.order === expectation.order
    );
  }
  if (expectation.kind === 'sector-money-flow') {
    const value = body as MarketSectorMoneyFlowRankingPage;
    return (
      (expectation.tradeDate === undefined || value.tradeDate === expectation.tradeDate) &&
      value.scheme === expectation.scheme &&
      value.order === expectation.order
    );
  }
  if (expectation.kind === 'sw-bars') {
    const value = body as SwIndustryBarPage;
    if (value.industry.code !== expectation.code || value.period !== expectation.period) {
      return false;
    }
    for (const item of value.items) {
      if (!dateInRange(item.periodEnd, expectation.start, expectation.end)) return false;
    }
    return true;
  }
  if (expectation.kind === 'sw-constituents') {
    const value = body as SwIndustryConstituentPage;
    return (
      value.industry.code === expectation.code &&
      (expectation.snapshotDate === undefined || value.snapshotDate === expectation.snapshotDate)
    );
  }
  const value = body as SwIndustryValuation;
  return (
    value.industry.code === expectation.code &&
    (expectation.tradeDate === undefined || value.tradeDate === expectation.tradeDate)
  );
}

/** 判断日历日期是否位于请求的包含端窗口内。 */
function dateInRange(value: string, start: string, end: string): boolean {
  return value >= start && value <= end;
}

/** 判断状态是否应计入市场读取断路器，而不是一次合法业务失败。 */
function isDependencyFailureStatus(status: number): boolean {
  return [401, 403, 500, 502, 503, 504].includes(status);
}

/** 读取有长度上限的 JSON 对象，防止异常下游响应耗尽 API 内存。 */
async function readJsonObject(response: Response, maximumBytes: number): Promise<unknown> {
  const declaredLength = Number(response.headers.get('content-length'));
  if (
    Number.isFinite(declaredLength) &&
    (!Number.isSafeInteger(declaredLength) || declaredLength > maximumBytes)
  ) {
    throw new Error('market response is too large');
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength > maximumBytes) throw new Error('market response is too large');
  return JSON.parse(new TextDecoder().decode(bytes)) as unknown;
}

/** 将内部依赖、鉴权、合同漂移和断路状态收敛为公开稳定 503。 */
function dependencyUnavailable(retryAfter?: number): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dataset-unavailable',
    'Published market data is temporarily unavailable',
    retryAfter,
  );
}

/** 将内部读取允许公开的状态映射为稳定错误，不透传下游详情。 */
async function upstreamProblem(response: Response): Promise<PublicProblemException> {
  const code = await safeProblemCode(response);
  if (response.status === 400) {
    return new PublicProblemException(
      HttpStatus.BAD_REQUEST,
      'validation-error',
      'Market data query is invalid',
    );
  }
  if (response.status === 404) {
    const publicCode = code === 'resource-not-found' ? code : 'publication-not-found';
    return new PublicProblemException(
      HttpStatus.NOT_FOUND,
      publicCode,
      'Requested market publication is not found',
    );
  }
  if (response.status === 409) {
    return new PublicProblemException(
      HttpStatus.CONFLICT,
      'cursor-mismatch',
      'Market data cursor does not match the requested publication',
    );
  }
  if (response.status === 424) {
    return new PublicProblemException(
      HttpStatus.FAILED_DEPENDENCY,
      'required-component-unavailable',
      'A required published market component is unavailable',
    );
  }
  if (response.status === 429) {
    return new PublicProblemException(
      HttpStatus.TOO_MANY_REQUESTS,
      'rate-limited',
      'Market data dependency is rate limited',
      retryAfterSeconds(response),
    );
  }
  return dependencyUnavailable(retryAfterSeconds(response));
}

/** 从下游 Problem 中只读取受限稳定代码，忽略详情与内部字段。 */
async function safeProblemCode(response: Response): Promise<string | undefined> {
  try {
    const value = await readJsonObject(response, 16 * 1_024);
    if (typeof value !== 'object' || value === null || !('code' in value)) return undefined;
    const code = value.code;
    return typeof code === 'string' && /^[a-z][a-z0-9-]{0,79}$/.test(code) ? code : undefined;
  } catch {
    return undefined;
  }
}

/** 读取受控 Retry-After 秒数，拒绝日期格式或无界数值。 */
function retryAfterSeconds(response: Response): number | undefined {
  const value = Number(response.headers.get('retry-after'));
  return Number.isSafeInteger(value) && value >= 1 && value <= 300 ? value : undefined;
}
