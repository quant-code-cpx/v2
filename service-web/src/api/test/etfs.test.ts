import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";

import {
  queryEtfDailyBars,
  queryEtfProfile,
  queryEtfProfiles,
  queryEtfTradingStates,
  queryEtfUnitNavs,
} from "../etfs";
import { authSession } from "../auth-session";
import { setHttpTransportForTests } from "../http";
import type { HttpTransportRequest } from "../http";
import type { EtfListFilters } from "../../types/etf";

/** 合同测试使用的固定非敏感身份、版本与关联标识。 */
const ids = {
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
  etf: "7ce0f18a-9f4d-4b3a-ae69-d0ff1707df91",
  request: "market/data:request-72",
  dataVersion: "8f401b48-5b0e-4a76-8d85-2c7101a28955",
} as const;

/** 返回登录合同需要的最小当前用户投影。 */
function currentUserPayload() {
  return {
    id: ids.user,
    account: "market.user",
    displayName: "市场用户",
    role: "USER",
    status: "ACTIVE",
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: "2026-07-30T00:00:00.000Z",
    updatedAt: "2026-07-30T00:00:00.000Z",
    permissions: [],
  };
}

/** 构造共享传输层可识别的 JSON 响应。 */
function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 解析共享 POST 传输层序列化的请求体。 */
function requestBody(request: HttpTransportRequest): Record<string, unknown> {
  if (typeof request.init.body !== "string") {
    throw new TypeError("ETF 查询请求必须包含 JSON body。");
  }
  const parsed: unknown = JSON.parse(request.init.body);
  if (typeof parsed !== "object" || parsed === null) {
    throw new TypeError("ETF 查询请求 body 必须为对象。");
  }

  return parsed as Record<string, unknown>;
}

/** 根据 dataset 代码返回严格 v2 values。 */
function valuesForDataset(datasetCode: string): Record<string, unknown> {
  if (datasetCode === "fund.etf.profile.reported") {
    return {
      etfEntityRef: ids.etf,
      exchange: "SSE",
      symbol: "510300",
      displayName: "沪深300ETF",
      etfType: "ETF",
      managementMode: "INDEX_TRACKING",
      managerName: "示例基金管理人",
      custodianName: null,
      listedOn: "2026-01-02",
      delistedOn: null,
      listingStatus: "LISTED",
      quoteCurrency: "CNY",
      navCurrency: "CNY",
      sourceTimePrecision: "DATE",
    };
  }
  if (datasetCode === "fund.etf.bar.1d.reported") {
    return {
      tradeDate: "2026-07-29",
      etfEntityRef: ids.etf,
      open: "3.900",
      high: "4.000",
      low: "3.880",
      close: "3.980",
      volume: "100",
      volumeUnit: "X".repeat(40),
      amount: "39800.00",
      currency: "CNY",
      tradeStatus: null,
      adjustment: "UNADJUSTED",
    };
  }
  if (datasetCode === "fund.etf.nav.1d.reported") {
    return {
      navDate: "2026-07-29",
      etfEntityRef: ids.etf,
      navKind: "UNIT",
      nav: "3.9700",
      currency: "CNY",
      finality: "FINAL",
    };
  }

  return {
    etfEntityRef: ids.etf,
    stateDimension: "SUBSCRIPTION",
    state: "OPEN",
    effectiveFrom: "2026-07-29",
    effectiveTo: null,
    reason: null,
  };
}

/** 构造符合标准 record envelope 的可用 publication 响应。 */
function marketDataResponse(body: Record<string, unknown>): Record<string, unknown> {
  const dataset = body.dataset as { code: string; schemaVersion: number };
  const page = body.page as { limit: number };

  return {
    meta: {
      requestId: ids.request,
      contractVersion: "1.0.0",
      dataset,
      availability: "AVAILABLE",
      release: {
        dataVersion: ids.dataVersion,
        publishedAt: "2026-07-30T01:00:00.000Z",
        knowledgeCutoff: "2026-07-30T00:30:00.000Z",
        publicUsableAt: "2026-07-30T01:00:00.000Z",
        effectiveFrom: null,
        effectiveTo: null,
        methodology: { code: "reported", version: "2", kind: "REPORTED" },
        sources: [
          {
            sourceRef: "src_approved",
            publisher: "已批准来源",
            sourceDataset: "ETF 数据",
            authoritative: true,
            redistribution: "INTERNAL_ONLY",
            coverageNote: null,
          },
        ],
        quality: { status: "PASSED", issueCodes: [] },
        completeness: "COMPLETE",
        disclaimers: [],
      },
      visibility: { mode: "CURRENT" },
      page: { limit: page.limit, hasMore: false, nextCursor: null },
      coverage: { from: null, to: null, pitCoverage: "COMPLETE", gaps: [] },
      warnings: [],
      disclaimers: [],
    },
    records: [
      {
        recordRef: `${dataset.code}:record`,
        recordType: "ETF",
        entity: { entityRef: ids.etf, entityType: "ETF", identifiers: [] },
        time: {},
        publicUsableAt: "2026-07-30T01:00:00.000Z",
        availabilityBasis: "SOURCE_PUBLICATION",
        sourcePublishedAt: null,
        observedAt: "2026-07-30T00:30:00.000Z",
        dataVersion: ids.dataVersion,
        sourceRef: "src_approved",
        methodologyVersion: "2",
        qualityStatus: "PASSED",
        revision: { revisionNumber: 1, currentInPublication: true },
        values: valuesForDataset(dataset.code),
      },
    ],
  };
}

/** 构造 availability、state 与公开 reason 显式一致的 ETF 非数据响应。 */
function emptyMarketDataResponse(
  body: Record<string, unknown>,
  availability: "EMPTY" | "SOURCE_UNAVAILABLE" | "CURRENTLY_UNSUPPORTED",
  reasonCode: string,
): Record<string, unknown> {
  const payload = marketDataResponse(body);
  const meta = payload.meta as Record<string, unknown>;
  meta.availability = availability;
  meta.release = {
    state: availability,
    observedAt: "2026-07-30T01:00:00.000Z",
    reasonCode,
  };
  meta.coverage = { from: null, to: null, pitCoverage: "UNKNOWN", gaps: [] };
  payload.records = [];
  return payload;
}

/** 通过真实内存会话取得 ETF 查询使用的 access token。 */
async function establishSession(): Promise<void> {
  await authSession.login({
    account: "market.user",
    password: "secure-pass-123",
    captchaId: ids.user,
    captchaAnswer: "1234",
  });
}

/** 返回 ETF 列表测试共用的 URL 查询状态。 */
function listFilters(q: string): EtfListFilters {
  return {
    exchange: "SSE",
    q,
    sort: "symbol",
    order: "asc",
    page: 1,
    pageSize: 50,
  };
}

describe("ETF typed market-data API", () => {
  /** 每个测试从无 access token 状态开始。 */
  beforeEach(() => {
    authSession.clear();
  });

  /** 每个测试恢复生产传输替身并清理内存会话。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
  });

  /** 四个 v2 dataset 经同一 POST 边界发送有界请求，日线和 NAV 上限固定为 366。 */
  it("sends all ETF v2 queries through the shared POST boundary", async () => {
    const requests: HttpTransportRequest[] = [];
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-market",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }

      return jsonResponse(200, marketDataResponse(requestBody(request)));
    });
    await establishSession();

    await queryEtfProfiles(listFilters("510"));
    await queryEtfProfiles(listFilters("沪深"));
    await queryEtfProfiles(listFilters("51030099"));
    await queryEtfProfile("SSE", "510300");
    await queryEtfDailyBars(ids.etf);
    await queryEtfUnitNavs(ids.etf);
    await queryEtfTradingStates(ids.etf);

    const marketRequests = requests.filter(
      /** 只保留公开 typed market-data 查询。 */
      (request) =>
        new URL(request.url, "http://apex.local").pathname === "/api/v1/market-data/query",
    );
    expect(marketRequests).toHaveLength(7);
    expect(
      marketRequests.every(
        /** 所有业务请求只能由共享传输层固定为 POST。 */
        (request) => request.init.method === "POST",
      ),
    ).toBe(true);
    expect(
      marketRequests.every(
        /** 所有 ETF 请求都必须携带当前内存 access token。 */
        (request) =>
          new Headers(request.init.headers).get("Authorization") === "Bearer access-market",
      ),
    ).toBe(true);

    const bodies = marketRequests.map(requestBody);
    expect(
      bodies.every((body) => (body.dataset as { schemaVersion: number }).schemaVersion === 2),
    ).toBe(true);
    expect(bodies[0]?.filters).toContainEqual({
      field: "symbol",
      operator: "PREFIX",
      values: ["510"],
    });
    expect(bodies[1]?.filters).toContainEqual({
      field: "displayName",
      operator: "CONTAINS",
      values: ["沪深"],
    });
    expect(bodies[2]?.filters).toContainEqual({
      field: "displayName",
      operator: "CONTAINS",
      values: ["51030099"],
    });
    expect(bodies[0]?.filters).not.toContainEqual(
      expect.objectContaining({ field: "displayName" }),
    );
    expect(bodies[1]?.filters).not.toContainEqual(expect.objectContaining({ field: "symbol" }));
    expect(bodies[2]?.filters).not.toContainEqual(expect.objectContaining({ field: "symbol" }));
    const barBody = bodies[4];
    const navBody = bodies[5];
    const stateBody = bodies[6];
    if (barBody === undefined || navBody === undefined || stateBody === undefined) {
      throw new Error("ETF 日线、NAV 或状态请求未生成。");
    }
    expect((barBody.page as { limit: number }).limit).toBe(366);
    expect((navBody.page as { limit: number }).limit).toBe(366);
    expect(navBody.filters).toContainEqual({
      field: "navKind",
      operator: "EQ",
      values: ["UNIT"],
    });
    expect((stateBody.page as { limit: number }).limit).toBe(500);
    expect(stateBody.sort).toEqual([{ field: "effectiveFrom", direction: "DESC" }]);
    const stateTime = stateBody.time as { from: string; to: string };
    expect(stateTime.from).not.toBe(stateTime.to);
  });

  /** 货币市场 ETF NAV 口径未冻结时接受明确不支持的成功空页，不映射为网络失败。 */
  it("accepts a currently-unsupported money-market NAV semantics page", async () => {
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-market",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }
      return jsonResponse(
        200,
        emptyMarketDataResponse(
          requestBody(request),
          "CURRENTLY_UNSUPPORTED",
          "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
        ),
      );
    });
    await establishSession();

    const result = await queryEtfUnitNavs(ids.etf);

    expect(result.meta.availability).toBe("CURRENTLY_UNSUPPORTED");
    expect(result.meta.release).toMatchObject({
      state: "CURRENTLY_UNSUPPORTED",
      reasonCode: "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
    });
    expect(result.records).toEqual([]);
  });

  /** Web 边界拒绝任意 reason、错误 state 和非 NAV dataset 的 CURRENTLY_UNSUPPORTED。 */
  it("rejects unavailable reasons that do not match ETF state or dataset", async () => {
    let availability: "EMPTY" | "SOURCE_UNAVAILABLE" | "CURRENTLY_UNSUPPORTED" =
      "CURRENTLY_UNSUPPORTED";
    let reasonCode = "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET";
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-market",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }
      return jsonResponse(
        200,
        emptyMarketDataResponse(requestBody(request), availability, reasonCode),
      );
    });
    await establishSession();

    await expect(queryEtfDailyBars(ids.etf)).rejects.toThrow();
    availability = "SOURCE_UNAVAILABLE";
    await expect(queryEtfUnitNavs(ids.etf)).rejects.toThrow();
    availability = "CURRENTLY_UNSUPPORTED";
    reasonCode = "PUBLICATION_NOT_AVAILABLE";
    await expect(queryEtfUnitNavs(ids.etf)).rejects.toThrow();
    availability = "EMPTY";
    reasonCode = "PROVIDER_UNAVAILABLE";
    await expect(queryEtfUnitNavs(ids.etf)).rejects.toThrow();
    availability = "SOURCE_UNAVAILABLE";
    reasonCode = "UNREVIEWED_DOWNSTREAM_REASON";
    await expect(queryEtfUnitNavs(ids.etf)).rejects.toThrow();
  });

  /** 扁平业务记录违反标准 envelope 时必须 fail-closed，不能当作真实 ETF 数据渲染。 */
  it("rejects a flat record that bypasses the standard envelope", async () => {
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-market",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }
      const body = requestBody(request);
      const payload = marketDataResponse(body);
      payload.records = [valuesForDataset("fund.etf.profile.reported")];
      return jsonResponse(200, payload);
    });
    await establishSession();

    await expect(queryEtfProfile("SSE", "510300")).rejects.toMatchObject({
      status: 502,
      code: "market-data-contract-mismatch",
    });
  });
});
