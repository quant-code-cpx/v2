import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";
import { InfiniteQueryObserver } from "@tanstack/react-query";

import {
  conditionalBody,
  equityBarsInfiniteQueryOptions,
  equityDataStatusQueryOptions,
  equityEventsQueryOptions,
  equitySearchQueryOptions,
  equitySectorsQueryOptions,
} from "../equity-market";
import { authSession } from "../auth-session";
import { queryClient } from "../query-client";
import { setHttpTransportForTests } from "../http";
import type { HttpTransportRequest } from "../http";
import { equityBarPageSchema, equitySearchResponseSchema } from "../../types/equity-market";

/** 固定非敏感 UUID，便于验证 publication 与条件缓存身份。 */
const identifiers = {
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
  dataVersion: "8f401b48-5b0e-4a76-8d85-2c7101a28955",
  coverageVersion: "a6fb18c8-c0e1-4c66-9f57-cf3e12d83f12",
  sourceBatchId: "ef4d71e4-a122-43d3-96e2-706ec55ff1ca",
} as const;

/** 返回登录合同需要的最小普通用户投影。 */
function currentUserPayload() {
  return {
    id: identifiers.user,
    account: "equity.user",
    displayName: "股票中心用户",
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
function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** 构造绑定同一测试 publication 的强条件响应头。 */
function conditionalHeaders(etag: string): Record<string, string> {
  return { ETag: etag, "X-Data-Version": identifiers.dataVersion };
}

/** 解析股票中心 POST 请求体。 */
function requestBody(request: HttpTransportRequest): Record<string, unknown> {
  if (typeof request.init.body !== "string") {
    throw new TypeError("股票中心请求必须包含 JSON body。");
  }
  const parsed: unknown = JSON.parse(request.init.body);
  if (typeof parsed !== "object" || parsed === null) {
    throw new TypeError("股票中心请求 body 必须为对象。");
  }
  return parsed as Record<string, unknown>;
}

/** 构造一个显式 PARTIAL、但仍可消费的真实合同形状 discovery publication。 */
function searchResponse() {
  return {
    availability: "AVAILABLE",
    reasonCode: null,
    release: {
      dataVersion: identifiers.dataVersion,
      publishedAt: "2026-07-30T01:00:00.000Z",
      effectiveAsOf: "2026-07-29",
      knowledgeCutoff: "2026-07-30T00:30:00.000Z",
      qualityStatus: "passed",
      completeness: "PARTIAL",
    },
    components: [
      {
        family: "VALUATION",
        dataVersion: null,
        availability: "UNAVAILABLE",
        sourceLabel: null,
        methodology: null,
      },
    ],
    capabilities: {
      sortFields: ["symbol", "changePercent"],
      columns: ["symbol", "name", "changePercent"],
      maxLimit: 100,
    },
    records: [
      {
        identity: {
          exchange: "SSE",
          symbol: "600000",
          name: "浦发银行",
          identityAsOf: "2026-07-29",
        },
        statuses: {
          listingStatus: "LISTED",
          tradingStatus: "TRADED",
          tradingStatusReason: null,
          listedOn: "1999-11-10",
          delistedOn: null,
        },
        market: {
          tradeDate: "2026-07-29",
          close: "10.25",
          previousClose: "10.00",
          changeAmount: "0.25",
          changePercent: "2.50",
          volumeShares: "1000000",
          amountCny: "10250000",
          turnoverRate: "0.20",
          currency: "CNY",
          nullReason: null,
        },
        capitalization: {
          effectiveOn: "2026-07-29",
          totalShares: "29352000000",
          listedTradableAShares: "29352000000",
          totalMarketCapCny: "300858000000",
          floatMarketCapCny: "300858000000",
          currency: "CNY",
          methodology: { code: "platform.unadjusted-close-cap", version: "1" },
          nullReason: null,
        },
        valuation: {
          tradeDate: null,
          peTtm: null,
          pb: null,
          psTtm: null,
          sourceLabel: null,
          methodology: null,
          nullReason: "NOT_COVERED",
        },
        moneyFlow: {
          tradeDate: "2026-07-29",
          netAmountCny: "1000000",
          netRatio: "1.25",
          sourceLabel: "Eastmoney",
          methodology: { code: "eastmoney.order-size-flow", version: "1" },
          nullReason: null,
        },
        memberships: [],
      },
    ],
    page: { nextCursor: "cursor-2", limit: 50 },
  };
}

/** 构造同一行情 publication 的一页真实合同形状。 */
function barPage(periodEnd: string, nextCursor: string | null) {
  return {
    exchange: "SSE",
    symbol: "600000",
    period: "1d",
    adjustmentMode: "none",
    adjustAsOf: null,
    factorVersion: null,
    formulaVersion: null,
    coverageVersion: identifiers.coverageVersion,
    publicationKind: "DATA",
    sourceBatchId: identifiers.sourceBatchId,
    dataVersion: identifiers.dataVersion,
    publishedAt: "2026-07-30T01:00:00.000Z",
    availability: "AVAILABLE",
    observedAt: null,
    reasonCode: null,
    qualityStatus: "passed",
    stale: false,
    items: [
      {
        periodEnd,
        open: "10.00",
        high: "10.50",
        low: "9.90",
        close: "10.25",
        volumeShares: "1000000",
        amountCny: "10250000",
        turnoverRate: "0.20",
        isFinal: true,
        revision: 1,
      },
    ],
    nextCursor,
  };
}

/** 通过真实内存会话取得股票中心查询使用的 access token。 */
async function establishSession(): Promise<void> {
  await authSession.login({
    account: "equity.user",
    password: "secure-pass-123",
    captchaId: identifiers.user,
    captchaAnswer: "1234",
  });
}

describe("equity market public API", () => {
  /** 每个测试从空会话和空事实缓存开始。 */
  beforeEach(() => {
    authSession.clear();
    queryClient.clear();
  });

  /** 每个测试恢复浏览器传输并清理内存状态。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
    queryClient.clear();
  });

  /** 行业叶查询必须把 publication 和代码复用身份同时放入请求与 TanStack Query 键。 */
  it("pins sector membership by dataVersion and identityAsOf", async () => {
    const requests: HttpTransportRequest[] = [];
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const requestUrl = new URL(request.url, "http://apex.local");
      const path = requestUrl.pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-equity-sector",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") return jsonResponse(200, currentUserPayload());
      return jsonResponse(
        200,
        {
          equity: {
            exchange: "SSE",
            symbol: "600000",
            name: "代码复用证券",
            listingStatus: "LISTED",
          },
          scheme: "eastmoney.industry",
          identityAsOf: requestUrl.searchParams.get("identityAsOf"),
          dataVersion: identifiers.dataVersion,
          release: {
            dataVersion: identifiers.dataVersion,
            publishedAt: "2026-07-30T01:00:00.000Z",
          },
          items: [],
          nextCursor: null,
        },
        conditionalHeaders('"equity-sectors-v1"'),
      );
    });
    await establishSession();

    await queryClient.fetchQuery(
      equitySectorsQueryOptions("SSE", "600000", "eastmoney.industry", {
        dataVersion: identifiers.dataVersion,
        identityAsOf: "2019-12-31",
      }),
    );
    await queryClient.fetchQuery(
      equitySectorsQueryOptions("SSE", "600000", "eastmoney.industry", {
        dataVersion: identifiers.dataVersion,
        identityAsOf: "2026-07-29",
      }),
    );

    const sectorUrls = requests
      .map((request) => new URL(request.url, "http://apex.local"))
      .filter((url) => url.pathname.endsWith("/market/equities/SSE/600000/sectors"));
    expect(sectorUrls).toHaveLength(2);
    expect(sectorUrls.map((url) => url.searchParams.get("identityAsOf"))).toEqual([
      "2019-12-31",
      "2026-07-29",
    ]);
    expect(
      sectorUrls.every((url) => url.searchParams.get("dataVersion") === identifiers.dataVersion),
    ).toBe(true);
    expect(sectorUrls.every((url) => !url.searchParams.has("asOf"))).toBe(true);
  });

  /** 返回另一身份日期时不得写入当前 Query key，避免代码复用证券污染缓存。 */
  it("rejects a sector membership response for another identity date", async () => {
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-equity-sector-mismatch",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") return jsonResponse(200, currentUserPayload());
      return jsonResponse(
        200,
        {
          equity: {
            exchange: "SSE",
            symbol: "600000",
            name: "代码复用证券",
            listingStatus: "LISTED",
          },
          scheme: "eastmoney.industry",
          identityAsOf: "2026-07-30",
          dataVersion: identifiers.dataVersion,
          release: {
            dataVersion: identifiers.dataVersion,
            publishedAt: "2026-07-30T01:00:00.000Z",
          },
          items: [],
          nextCursor: null,
        },
        conditionalHeaders('"equity-sectors-mismatch"'),
      );
    });
    await establishSession();

    await expect(
      queryClient.fetchQuery(
        equitySectorsQueryOptions("SSE", "600000", "eastmoney.industry", {
          dataVersion: identifiers.dataVersion,
          identityAsOf: "2026-07-29",
        }),
      ),
    ).rejects.toMatchObject({
      status: 409,
      code: "snapshot-expired",
    });
  });

  /** 搜索、事件和 18-family 状态都通过认证 POST 发送精确请求体。 */
  it("uses the authenticated POST boundary for search, events, and all data statuses", async () => {
    const requests: HttpTransportRequest[] = [];
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-equity",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") return jsonResponse(200, currentUserPayload());
      if (path === "/api/v1/equities/search") {
        return jsonResponse(200, searchResponse(), conditionalHeaders('"equity-search-v1"'));
      }
      if (path.endsWith("/events/search")) {
        return jsonResponse(
          200,
          {
            availability: "AVAILABLE",
            reasonCode: null,
            release: {
              dataVersion: identifiers.dataVersion,
              publishedAt: "2026-07-30T01:00:00.000Z",
              effectiveAsOf: "2026-07-29",
              knowledgeCutoff: "2026-07-30T00:30:00.000Z",
              qualityStatus: "passed",
            },
            events: [],
            page: { nextCursor: null, limit: 50 },
          },
          conditionalHeaders('"equity-events-v1"'),
        );
      }
      return jsonResponse(
        200,
        {
          identity: {
            exchange: "SSE",
            symbol: "600000",
            name: "浦发银行",
            identityAsOf: "2026-07-29",
          },
          datasets: [],
        },
        conditionalHeaders('"equity-status-v1"'),
      );
    });
    await establishSession();

    await queryClient.fetchQuery(
      equitySearchQueryOptions({
        q: "600000",
        exchanges: ["SSE"],
        sort: [{ field: "changePercent", direction: "DESC" }],
        limit: 50,
      }),
    );
    await queryClient.fetchQuery(
      equityEventsQueryOptions("SSE", "600000", {
        families: ["DRAGON_TIGER", "BLOCK_TRADE"],
        asOf: "2026-07-29",
        start: "2025-07-30",
        end: "2026-07-29",
        limit: 50,
      }),
    );
    const families = [
      "IDENTITY",
      "COMPANY_PROFILE",
      "BARS_1D",
      "BARS_1W",
      "BARS_1MO",
      "ADJUSTMENT_FACTOR",
      "CORPORATE_ACTION",
      "FINANCIAL_REPORT",
      "FINANCIAL_INDICATOR",
      "VALUATION",
      "MONEY_FLOW",
      "INDUSTRY_MEMBERSHIP",
      "CONCEPT_MEMBERSHIP",
      "SW_INDUSTRY_MEMBERSHIP",
      "EARNINGS_FORECAST",
      "EARNINGS_EXPRESS",
      "DRAGON_TIGER",
      "BLOCK_TRADE",
    ] as const;
    await queryClient.fetchQuery(equityDataStatusQueryOptions("SSE", "600000", { families }));

    const equityRequests = requests.filter(
      /** 登录请求不属于股票中心业务合同。 */
      (request) =>
        new URL(request.url, "http://apex.local").pathname.startsWith("/api/v1/equities/"),
    );
    expect(
      equityRequests.map(
        /** 路径顺序对应三次 Query 调用。 */
        (request) => new URL(request.url, "http://apex.local").pathname,
      ),
    ).toEqual([
      "/api/v1/equities/search",
      "/api/v1/equities/SSE/600000/events/search",
      "/api/v1/equities/SSE/600000/data-status",
    ]);
    expect(
      equityRequests.every(
        /** 所有业务调用都使用共享 POST 与当前 Bearer token。 */
        (request) =>
          request.init.method === "POST" &&
          new Headers(request.init.headers).get("Authorization") === "Bearer access-equity",
      ),
    ).toBe(true);
    expect(requestBody(equityRequests[0] as HttpTransportRequest)).toEqual({
      q: "600000",
      exchanges: ["SSE"],
      sort: [{ field: "changePercent", direction: "DESC" }],
      limit: 50,
    });
    expect(requestBody(equityRequests[1] as HttpTransportRequest)).toEqual({
      families: ["DRAGON_TIGER", "BLOCK_TRADE"],
      asOf: "2026-07-29",
      start: "2025-07-30",
      end: "2026-07-29",
      limit: 50,
    });
    expect(requestBody(equityRequests[2] as HttpTransportRequest)).toEqual({ families });
  });

  /** ETag 命中的 POST 204 必须保留 Query cache 内同一 publication 实体。 */
  it("keeps the validated entity when a conditional refresh returns 204", async () => {
    let searchCalls = 0;
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-equity",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") return jsonResponse(200, currentUserPayload());
      searchCalls += 1;
      if (searchCalls === 1) {
        return jsonResponse(200, searchResponse(), conditionalHeaders('"equity-search-v1"'));
      }
      expect(new Headers(request.init.headers).get("If-None-Match")).toBe('"equity-search-v1"');
      return jsonResponse(204, undefined, conditionalHeaders('"equity-search-v1"'));
    });
    await establishSession();

    const options = equitySearchQueryOptions({
      sort: [{ field: "symbol", direction: "ASC" }],
      limit: 50,
    });
    const first = await queryClient.fetchQuery(options);
    await queryClient.invalidateQueries({ queryKey: options.queryKey });
    const second = await queryClient.fetchQuery(options);

    expect(conditionalBody(second)).toEqual(conditionalBody(first));
    expect(searchCalls).toBe(2);
  });

  /** 没有 discovery publication 时应消费显式 UNAVAILABLE 信封，不能要求虚构版本头。 */
  it("accepts the explicit no-publication search envelope without cache headers", async () => {
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://apex.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-equity",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") return jsonResponse(200, currentUserPayload());
      return jsonResponse(200, {
        availability: "UNAVAILABLE",
        reasonCode: "NO_PUBLICATION",
        release: null,
        components: [],
        capabilities: {
          sortFields: ["symbol"],
          columns: ["symbol", "name"],
          maxLimit: 100,
        },
        records: [],
        page: { nextCursor: null, limit: 50 },
      });
    });
    await establishSession();

    const result = await queryClient.fetchQuery(
      equitySearchQueryOptions({
        sort: [{ field: "symbol", direction: "ASC" }],
        limit: 50,
      }),
    );

    expect(conditionalBody(result)).toMatchObject({
      availability: "UNAVAILABLE",
      reasonCode: "NO_PUBLICATION",
      release: null,
      records: [],
    });
    expect(result.etag).toBeUndefined();
    expect(result.dataVersion).toBeUndefined();
  });

  /** K 线 Infinite Query 必须透传 cursor，并只暴露同一 publication 的递增合并序列。 */
  it("loads and merges a signed K-line cursor chain", async () => {
    const barRequests: HttpTransportRequest[] = [];
    setHttpTransportForTests(async (request) => {
      const url = new URL(request.url, "http://apex.local");
      if (url.pathname === "/api/v1/auth/login") {
        return jsonResponse(200, {
          accessToken: "access-equity",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (url.pathname === "/api/v1/users/me") {
        return jsonResponse(200, currentUserPayload());
      }
      barRequests.push(request);
      return url.searchParams.get("cursor") === null
        ? jsonResponse(
            200,
            barPage("2026-07-28", "signed-cursor-2"),
            conditionalHeaders('"equity-bars-page-1"'),
          )
        : jsonResponse(
            200,
            barPage("2026-07-29", null),
            conditionalHeaders('"equity-bars-page-2"'),
          );
    });
    await establishSession();

    const observer = new InfiniteQueryObserver(
      queryClient,
      equityBarsInfiniteQueryOptions("SSE", "600000", {
        dataVersion: identifiers.dataVersion,
        period: "1d",
        start: "2026-07-28",
        end: "2026-07-29",
        adjust: "none",
        limit: 1,
      }),
    );
    await observer.refetch();
    await observer.fetchNextPage();
    const result = observer.getCurrentResult();
    observer.destroy();

    expect(
      result.data?.items.map(
        /** 合并结果必须维持服务端升序。 */
        (item) => item.periodEnd,
      ),
    ).toEqual(["2026-07-28", "2026-07-29"]);
    expect(
      new URL(barRequests[0]?.url ?? "", "http://apex.local").searchParams.get("dataVersion"),
    ).toBe(identifiers.dataVersion);
    expect(new URL(barRequests[1]?.url ?? "", "http://apex.local").searchParams.get("cursor")).toBe(
      "signed-cursor-2",
    );
    expect(result.data).toMatchObject({
      coverageVersion: identifiers.coverageVersion,
      publicationKind: "DATA",
      sourceBatchId: identifiers.sourceBatchId,
    });
  });

  /** K 线只接受带精确覆盖谱系的公开合同，零记录与数据页不能互相伪装。 */
  it("requires exact coverage lineage and a valid publication shape for bars", () => {
    const data = barPage("2026-07-28", null);
    const zeroRecordCoverage = {
      ...data,
      publicationKind: "ZERO_RECORD_COVERAGE",
      items: [],
    };
    const missingCoverage = { ...data } as Record<string, unknown>;
    delete missingCoverage.coverageVersion;

    expect(equityBarPageSchema.parse(data)).toMatchObject({
      coverageVersion: identifiers.coverageVersion,
      publicationKind: "DATA",
      sourceBatchId: identifiers.sourceBatchId,
    });
    expect(equityBarPageSchema.parse(zeroRecordCoverage).publicationKind).toBe(
      "ZERO_RECORD_COVERAGE",
    );
    expect(() => equityBarPageSchema.parse(missingCoverage)).toThrow();
    expect(() =>
      equityBarPageSchema.parse({
        ...data,
        securityId: "b8dcd29a-0ec5-4e4e-a8cf-1768a5c4a980",
      }),
    ).toThrow();
    expect(() =>
      equityBarPageSchema.parse({ ...data, formulaVersion: "unapproved-formula-v1" }),
    ).toThrow();
    expect(() => equityBarPageSchema.parse({ ...data, items: [] })).toThrow();
    expect(() => equityBarPageSchema.parse({ ...zeroRecordCoverage, items: data.items })).toThrow();
  });

  /** 搜索 response 没有显式 completeness 时必须 fail-closed。 */
  it("rejects a search publication without explicit completeness", () => {
    const response = searchResponse();
    const release = { ...response.release } as Record<string, unknown>;
    delete release.completeness;

    expect(() => equitySearchResponseSchema.parse({ ...response, release })).toThrow();
  });
});
