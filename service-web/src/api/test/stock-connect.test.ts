import { createHash } from "node:crypto";

import { afterEach, beforeEach, describe, expect, it } from "vite-plus/test";

import {
  queryStockConnectActiveSecurities,
  queryStockConnectChannel,
  queryStockConnectOverview,
  queryStockConnectReadiness,
  queryStockConnectSecurityContext,
} from "../stock-connect";
import { authSession } from "../auth-session";
import { setHttpTransportForTests } from "../http";
import type { HttpTransportRequest } from "../http";
import {
  stockConnectActiveSecurityQuerySchema,
  stockConnectOverviewResponseSchema,
  stockConnectReadinessResponseSchema,
} from "../../types/stock-connect";

/** 固定非敏感合同标识，便于验证请求与条件缓存。 */
const contractIdentifiers = {
  user: "16b6bc36-b3ec-4e8c-b2c8-9f704a83d415",
  bundle: "635f6863-7008-4bcf-a69f-3e58e302b72c",
  version: "bundle-v1",
  etag: '"bundle-v1"',
  tradeDate: "2026-07-30",
  instrument: "instrument:stock-connect:001",
} as const;

/** 返回登录和身份验证流程所需的最小用户公开投影。 */
function currentUserPayload() {
  return {
    id: contractIdentifiers.user,
    account: "contract.user",
    displayName: "合同用户",
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

/** 构造共享 transport 可识别的 JSON 响应。 */
function jsonResponse(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
  });
}

/** 构造带标准 ETag 与 dataVersion 的成功业务响应。 */
function stockConnectResponse(
  body: unknown,
  dataVersion: string = contractIdentifiers.version,
): Response {
  return jsonResponse(body, {
    ETag: contractIdentifiers.etag,
    "X-Data-Version": dataVersion,
  });
}

/** 构造 204 条件命中响应，实体保持在 TanStack Query 或调用方缓存。 */
function notModifiedResponse(): Response {
  return new Response(null, {
    status: 204,
    headers: {
      ETag: contractIdentifiers.etag,
      "X-Data-Version": contractIdentifiers.version,
    },
  });
}

/** 解析共享 HTTP 传输已经序列化的 JSON 请求体。 */
function requestBody(request: HttpTransportRequest): unknown {
  if (typeof request.init.body !== "string") {
    throw new TypeError("测试请求缺少 JSON 字符串 body。");
  }

  return JSON.parse(request.init.body) as unknown;
}

/** 返回制度未披露且绝不以零替代的金额事实。 */
function undisclosedMoneyFact() {
  return {
    availability: "NOT_DISCLOSED_BY_REGIME",
    value: null,
    lineageRef: null,
  };
}

/** 返回带原币和 lineage 的已报告金额事实。 */
function reportedMoneyFact(amount = "100.00", currency: "CNY" | "HKD" = "CNY") {
  return {
    availability: "REPORTED",
    value: { amount, currency, unit: "BASE" },
    lineageRef: "lineage:reported",
  };
}

/** 返回带输入 lineage 的同源买卖差额。 */
function derivedMoneyFact(amount = "10.00", currency: "CNY" | "HKD" = "CNY") {
  return {
    availability: "DERIVED",
    value: { amount, currency, unit: "BASE" },
    lineageRef: "lineage:reported:buy-minus-sell-v1",
  };
}

/** 返回满足公开合同的市场统计字段集。 */
function marketStats() {
  return {
    buyAmount: undisclosedMoneyFact(),
    sellAmount: undisclosedMoneyFact(),
    turnoverAmount: reportedMoneyFact(),
    netBuyAmount: undisclosedMoneyFact(),
    tradeCount: {
      availability: "REPORTED",
      value: 10,
      lineageRef: "lineage:count",
    },
    etfTurnoverAmount: undisclosedMoneyFact(),
  };
}

/** 返回额度充足但制度不披露具体阈值以上余额的日终状态。 */
function channelStatus() {
  return {
    tradingDay: true,
    sessionState: "CLOSED",
    buyOrderAccepted: true,
    sellOrderAccepted: true,
    quotaState: "SUFFICIENT",
    quotaBalance: undisclosedMoneyFact(),
    observedAt: "2026-07-30T18:00:00+08:00",
    finality: "END_OF_DAY",
  };
}

/** 返回一次带官方来源和北向状态派生警告的不可变 publication。 */
function publication() {
  return {
    bundleReleaseId: contractIdentifiers.bundle,
    dataVersion: contractIdentifiers.version,
    tradeDate: contractIdentifiers.tradeDate,
    publishedAt: "2026-07-30T18:15:00+08:00",
    qualityStatus: "APPROVED_WITH_WARNINGS",
    qualityIssues: [
      {
        code: "SESSION_STATE_DERIVED_FROM_CALENDAR_AND_FINALITY",
        component: "SH_NORTHBOUND.status",
        detail: "北向日终状态由官方开放日与 END_OF_DAY finality 联合确定。",
      },
    ],
    sourceRefs: [
      {
        sourceCode: "HKEX_DATA_MARKETPLACE",
        productName: "Stock Connect Daily Statistics",
        sourcePublicationAvailability: "REPORTED",
        sourcePublicationAt: "2026-07-30T18:00:00+08:00",
        sourceObservedAt: "2026-07-30T18:03:00+08:00",
        sourceFileSha256: "a".repeat(64),
      },
    ],
  };
}

/** 返回由正式日历与 bundle publication 支持的独立 readiness 快照。 */
function readinessBody() {
  const body = {
    schemaVersion: "quant-v2.stock-connect-readiness.v1",
    mode: "EXACT",
    selectedChannels: ["SH_NORTHBOUND"],
    requestedExactDate: contractIdentifiers.tradeDate,
    candidateTradeDate: contractIdentifiers.tradeDate,
    readyTradeDate: contractIdentifiers.tradeDate,
    observedAt: "2026-07-30T18:15:00+08:00",
    calendar: {
      dataVersion: "b".repeat(64),
      observedAt: "2026-07-30T08:00:00+08:00",
      sourceFileSha256: "c".repeat(64),
      sourcePublicationAt: null,
      publicationAvailability: "NOT_REPORTED",
    },
    channels: [
      {
        channel: "SH_NORTHBOUND",
        calendarState: "OPEN",
        state: "READY",
        reasonCode: "BUNDLE_PUBLISHED",
        bundleDataVersion: contractIdentifiers.version,
        evidenceObservedAt: "2026-07-30T18:15:00+08:00",
      },
    ],
  };
  return {
    ...body,
    dataVersion: createHash("sha256").update(canonicalJson(body), "utf8").digest("hex"),
  };
}

/** 返回与 Python、Node API 共享的 Unicode、null 与数组 readiness 固定向量。 */
function readinessCrossLanguageVector() {
  return {
    schemaVersion: "quant-v2.stock-connect-readiness.v1",
    dataVersion: "abe5d1926e56f9f60959b27141e450ad1a0f580437e59a8e737a1efe34276307",
    mode: "EXACT",
    selectedChannels: ["SH_NORTHBOUND", "SZ_SOUTHBOUND"],
    requestedExactDate: "2026-07-30",
    candidateTradeDate: "2026-07-30",
    readyTradeDate: null,
    observedAt: "2026-07-30T10:00:00Z",
    calendar: {
      dataVersion: "a".repeat(64),
      observedAt: null,
      sourceFileSha256: null,
      sourcePublicationAt: null,
      publicationAvailability: "SOURCE_MISSING",
    },
    channels: [
      {
        channel: "SH_NORTHBOUND",
        calendarState: "OPEN",
        state: "READY",
        reasonCode: "BUNDLE_PUBLISHED",
        bundleDataVersion: "版本-α",
        evidenceObservedAt: "2026-07-30T10:00:00Z",
      },
      {
        channel: "SZ_SOUTHBOUND",
        calendarState: "UNKNOWN",
        state: "SOURCE_MISSING",
        reasonCode: "CALENDAR_SOURCE_MISSING",
        bundleDataVersion: null,
        evidenceObservedAt: "2026-07-30T10:00:00Z",
      },
    ],
  };
}

/** 返回沪股通通道摘要。 */
function channelSummary() {
  return {
    channel: "SH_NORTHBOUND",
    direction: "NORTHBOUND",
    route: "SHANGHAI",
    tradeDate: contractIdentifiers.tradeDate,
    stats: marketStats(),
    status: channelStatus(),
    activeSecurityCount: 1,
  };
}

/** 返回 readiness 与四个业务公开端点各自的严格合同实体。 */
function responseBodies() {
  const trendPoint = {
    channel: "SH_NORTHBOUND",
    tradeDate: contractIdentifiers.tradeDate,
    dataVersion: contractIdentifiers.version,
    stats: marketStats(),
    status: channelStatus(),
  };
  const identity = {
    identityAvailability: "RESOLVED",
    instrumentEntityRef: contractIdentifiers.instrument,
    sourceSecurityCode: "001",
    displayName: "合同证券",
    listingVenue: "SSE",
  };
  const activeItem = {
    rankingRank: 1,
    sourceRank: 1,
    identity,
    buyAmount: undisclosedMoneyFact(),
    sellAmount: undisclosedMoneyFact(),
    turnoverAmount: reportedMoneyFact(),
    netBuyAmount: undisclosedMoneyFact(),
  };

  return {
    overview: {
      resolvedTradeDate: contractIdentifiers.tradeDate,
      dateResolution: "EXACT",
      channels: [channelSummary()],
      trend: [trendPoint],
      publication: publication(),
    },
    readiness: readinessBody(),
    channel: {
      resolvedTradeDate: contractIdentifiers.tradeDate,
      dateResolution: "EXACT",
      channel: channelSummary(),
      trend: [trendPoint],
      publication: publication(),
    },
    active: {
      resolvedTradeDate: contractIdentifiers.tradeDate,
      dateResolution: "EXACT",
      channel: "SH_NORTHBOUND",
      ranking: "SOURCE_ACTIVE",
      rankingAvailability: "REPORTED",
      rankingScope: "SOURCE_ACTIVE_SECURITIES_ONLY",
      items: [activeItem],
      nextCursor: null,
      publication: publication(),
    },
    security: {
      resolvedTradeDate: contractIdentifiers.tradeDate,
      identity,
      activities: [
        {
          channel: "SH_NORTHBOUND",
          tradeDate: contractIdentifiers.tradeDate,
          dataVersion: contractIdentifiers.version,
          sourceRank: 1,
          turnoverAmount: reportedMoneyFact(),
          netBuyAmount: undisclosedMoneyFact(),
        },
      ],
      publication: publication(),
    },
  };
}

/** 通过真实内存会话取得测试请求的 Bearer token。 */
async function establishSession(): Promise<void> {
  await authSession.login({
    account: "contract.user",
    password: "contract-password",
    captchaId: contractIdentifiers.user,
    captchaAnswer: "1234",
  });
}

/** 验证互联互通前端只接受严格公开合同与同键条件缓存。 */
describe("stock connect API", () => {
  /** 每个合同测试从无 token、无业务缓存状态开始。 */
  beforeEach(() => {
    authSession.clear();
  });

  /** 每个合同测试恢复浏览器 transport 并清理用户隔离缓存。 */
  afterEach(() => {
    setHttpTransportForTests();
    authSession.clear();
  });

  /** readiness 与四个业务读取只走共享 POST、Bearer 与冻结请求体。 */
  it("maps all public POST routes and validates publication responses", async () => {
    const requests: HttpTransportRequest[] = [];
    const bodies = responseBodies();

    /** 以公开路径返回各自严格响应，同时记录浏览器传输输入。 */
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }
      if (path.endsWith("/overview/query")) {
        return stockConnectResponse(bodies.overview);
      }
      if (path.endsWith("/readiness/query")) {
        return stockConnectResponse(bodies.readiness, bodies.readiness.dataVersion);
      }
      if (path.endsWith("/channels/query")) {
        return stockConnectResponse(bodies.channel);
      }
      if (path.endsWith("/active-securities/query")) {
        return stockConnectResponse(bodies.active);
      }

      return stockConnectResponse(bodies.security);
    });
    await establishSession();
    const date = {
      mode: "EXACT" as const,
      exactDate: contractIdentifiers.tradeDate,
    };

    const overview = await queryStockConnectOverview({
      date,
      channels: ["SH_NORTHBOUND"],
      trendTradingDays: 20,
    });
    const readiness = await queryStockConnectReadiness({
      date,
      channels: ["SH_NORTHBOUND"],
    });
    await queryStockConnectChannel({
      date,
      channel: "SH_NORTHBOUND",
      trendTradingDays: 20,
    });
    await queryStockConnectActiveSecurities({
      date,
      channel: "SH_NORTHBOUND",
      ranking: "SOURCE_ACTIVE",
      parentPublicationDataVersion: contractIdentifiers.version,
      cursor: null,
      limit: 20,
    });
    await queryStockConnectSecurityContext({
      instrumentEntityRef: contractIdentifiers.instrument,
      date,
      channel: null,
      historyTradingDays: 20,
    });

    const businessRequests = requests.filter(
      /** 只提取互联互通业务请求，排除会话建立 POST。 */
      (request) =>
        new URL(request.url, "http://quant.local").pathname.startsWith(
          "/api/v1/market/stock-connect/",
        ),
    );
    expect(
      businessRequests.map(
        /** 提取公开业务路径用于精确顺序断言。 */
        (request) => new URL(request.url, "http://quant.local").pathname,
      ),
    ).toEqual([
      "/api/v1/market/stock-connect/overview/query",
      "/api/v1/market/stock-connect/readiness/query",
      "/api/v1/market/stock-connect/channels/query",
      "/api/v1/market/stock-connect/active-securities/query",
      "/api/v1/market/stock-connect/securities/context/query",
    ]);
    expect(
      businessRequests.every(
        /** 所有互联互通业务请求都必须使用共享传输层固定的 POST。 */
        (request) => request.init.method === "POST",
      ),
    ).toBe(true);
    expect(
      businessRequests.every(
        /** 所有业务读取都必须携带会话协调器提供的 Bearer token。 */
        (request) =>
          new Headers(request.init.headers).get("Authorization") === "Bearer access-contract",
      ),
    ).toBe(true);
    const activeRequest = businessRequests[3];
    if (activeRequest === undefined) {
      throw new Error("缺少活跃证券请求。");
    }
    expect(requestBody(activeRequest)).toMatchObject({
      parentPublicationDataVersion: contractIdentifiers.version,
    });
    expect(overview.dataVersion).toBe(contractIdentifiers.version);
    expect(overview.etag).toBe(contractIdentifiers.etag);
    expect(readiness.dataVersion).toBe(bodies.readiness.dataVersion);
  });

  /** 验证浏览器 Web Crypto 与 Python、Node API 对同一规范 JSON 得到固定摘要。 */
  it("matches the frozen cross-language readiness digest vector", async () => {
    const vector = readinessCrossLanguageVector();

    /** 只为该固定向量提供真实公开路由形状；会话仍经共享 transport 建立。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") return jsonResponse(currentUserPayload());
      return stockConnectResponse(vector, vector.dataVersion);
    });
    await establishSession();

    const result = await queryStockConnectReadiness({
      date: { mode: "EXACT", exactDate: "2026-07-30" },
      channels: ["SH_NORTHBOUND", "SZ_SOUTHBOUND"],
    });

    expect(result.dataVersion).toBe(
      "abe5d1926e56f9f60959b27141e450ad1a0f580437e59a8e737a1efe34276307",
    );
  });

  /** 总览父 publication 的 latest 活跃榜必须接受共同完成日解析语义。 */
  it("accepts LATEST_COMMON for an active page bound to an overview publication", async () => {
    const bodies = responseBodies();

    /** 建立会话后返回绑定总览父版本的 latest 活跃榜。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }
      return stockConnectResponse({
        ...bodies.active,
        dateResolution: "LATEST_COMMON",
      });
    });
    await establishSession();

    const response = await queryStockConnectActiveSecurities({
      date: { mode: "LATEST", exactDate: null },
      channel: "SH_NORTHBOUND",
      ranking: "SOURCE_ACTIVE",
      parentPublicationDataVersion: contractIdentifiers.version,
      cursor: null,
      limit: 20,
    });

    expect(response.data.dateResolution).toBe("LATEST_COMMON");
  });

  /** 榜单响应必须再次绑定调用方请求的父 publication，不能只信任成功状态和响应头。 */
  it("rejects an active page from a different parent publication", async () => {
    const bodies = responseBodies();
    const differentVersion = "bundle-v2";
    const mismatchedActivePage = {
      ...bodies.active,
      publication: {
        ...bodies.active.publication,
        dataVersion: differentVersion,
      },
    };

    /** 返回内部自洽但不属于请求父版本的榜单，验证浏览器最后一道跨版本防线。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }

      return jsonResponse(mismatchedActivePage, {
        ETag: `"${differentVersion}"`,
        "X-Data-Version": differentVersion,
      });
    });
    await establishSession();

    await expect(
      queryStockConnectActiveSecurities({
        date: { mode: "EXACT", exactDate: contractIdentifiers.tradeDate },
        channel: "SH_NORTHBOUND",
        ranking: "SOURCE_ACTIVE",
        parentPublicationDataVersion: contractIdentifiers.version,
        cursor: null,
        limit: 20,
      }),
    ).rejects.toMatchObject({
      status: 502,
      code: "stock-connect-response-scope-mismatch",
    });
  });

  /** 204 条件命中必须发送带引号 ETag 并保留同一缓存实体。 */
  it("keeps cached body on a standard ETag 204 response", async () => {
    const requests: HttpTransportRequest[] = [];
    const bodies = responseBodies();
    let overviewReads = 0;

    /** 第一次返回实体，第二次用同 ETag 返回严格 204。 */
    setHttpTransportForTests(async (request) => {
      requests.push(request);
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }
      overviewReads += 1;
      return overviewReads === 1 ? stockConnectResponse(bodies.overview) : notModifiedResponse();
    });
    await establishSession();
    const request = {
      date: {
        mode: "EXACT" as const,
        exactDate: contractIdentifiers.tradeDate,
      },
      channels: ["SH_NORTHBOUND" as const],
      trendTradingDays: 20,
    };

    const first = await queryStockConnectOverview(request);
    const second = await queryStockConnectOverview(request, { previous: first });
    const conditionalRequest = requests.at(-1);

    expect(second).toBe(first);
    expect(new Headers(conditionalRequest?.init.headers).get("If-None-Match")).toBe(
      contractIdentifiers.etag,
    );
  });

  /** 200 成功响应不得借用旧缓存掩盖空 body，只有严格 204 可以复用。 */
  it("rejects an empty 200 response instead of treating it as not modified", async () => {
    /** 认证成功后故意返回带版本头但没有实体的 200。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }

      return new Response(null, {
        status: 200,
        headers: {
          ETag: contractIdentifiers.etag,
          "X-Data-Version": contractIdentifiers.version,
        },
      });
    });
    await establishSession();

    await expect(
      queryStockConnectOverview({
        date: { mode: "LATEST", exactDate: null },
        channels: ["SH_NORTHBOUND"],
        trendTradingDays: 20,
      }),
    ).rejects.toMatchObject({
      status: 502,
      code: "stock-connect-success-body-missing",
    });
  });

  /** 来源榜必须是 REPORTED；净额榜只有 DERIVED 才可携带记录或下一游标。 */
  it("fails closed when ranking availability would expose a fallback list", async () => {
    const bodies = responseBodies();
    bodies.active.ranking = "NET_BUY";
    bodies.active.rankingAvailability = "REPORTED";

    /** 认证成功后返回当前可变的严格榜单测试载荷。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }

      return stockConnectResponse(bodies.active);
    });
    await establishSession();

    await expect(
      queryStockConnectActiveSecurities({
        date: { mode: "EXACT", exactDate: contractIdentifiers.tradeDate },
        channel: "SH_NORTHBOUND",
        ranking: "NET_BUY",
        parentPublicationDataVersion: contractIdentifiers.version,
        cursor: null,
        limit: 20,
      }),
    ).rejects.toMatchObject({
      status: 502,
      code: "stock-connect-response-scope-mismatch",
    });

    bodies.active.rankingAvailability = "NOT_DISCLOSED_BY_REGIME";
    bodies.active.items = [];
    await expect(
      queryStockConnectActiveSecurities({
        date: { mode: "EXACT", exactDate: contractIdentifiers.tradeDate },
        channel: "SH_NORTHBOUND",
        ranking: "NET_BUY",
        parentPublicationDataVersion: contractIdentifiers.version,
        cursor: null,
        limit: 20,
      }),
    ).resolves.toMatchObject({
      data: {
        rankingAvailability: "NOT_DISCLOSED_BY_REGIME",
        items: [],
        nextCursor: null,
      },
    });

    bodies.active.ranking = "SOURCE_ACTIVE";
    bodies.active.rankingAvailability = "SOURCE_MISSING";
    await expect(
      queryStockConnectActiveSecurities({
        date: { mode: "EXACT", exactDate: contractIdentifiers.tradeDate },
        channel: "SH_NORTHBOUND",
        ranking: "SOURCE_ACTIVE",
        parentPublicationDataVersion: contractIdentifiers.version,
        cursor: null,
        limit: 20,
      }),
    ).rejects.toMatchObject({
      status: 502,
      code: "stock-connect-response-scope-mismatch",
    });
  });

  /** 父版本是可扩展不透明标识而非 UUID，但不得包含控制字符。 */
  it("accepts opaque parent versions and rejects control characters", () => {
    const request = {
      date: { mode: "LATEST", exactDate: null },
      channel: "SH_NORTHBOUND",
      ranking: "SOURCE_ACTIVE",
      cursor: null,
      limit: 20,
    } as const;

    expect(
      stockConnectActiveSecurityQuerySchema.safeParse({
        ...request,
        parentPublicationDataVersion: "stock-connect.2026-07-29.revision-1",
      }).success,
    ).toBe(true);
    expect(
      stockConnectActiveSecurityQuerySchema.safeParse({
        ...request,
        parentPublicationDataVersion: "stock-connect\nforged",
      }).success,
    ).toBe(false);
  });

  /** readiness observedAt 必须是证据最大时间，不能替换为查询当前时间。 */
  it("rejects a readiness timestamp synthesized after its persisted evidence", () => {
    const response = readinessBody();
    response.observedAt = "2026-07-30T18:16:00+08:00";

    expect(stockConnectReadinessResponseSchema.safeParse(response).success).toBe(false);
  });

  /** 浏览器边界再次拒绝负成交额、伪造净额及不满足精确恒等式的金额。 */
  it("validates money provenance, signs and identities before rendering", () => {
    const bodies = responseBodies();
    const summary = bodies.overview.channels[0];
    if (summary === undefined) {
      throw new Error("合同测试必须包含通道摘要。");
    }

    const validStats = {
      ...summary.stats,
      buyAmount: reportedMoneyFact("40.00"),
      sellAmount: reportedMoneyFact("60.00"),
      turnoverAmount: reportedMoneyFact("100.00"),
      netBuyAmount: derivedMoneyFact("-20.00"),
    };
    expect(
      stockConnectOverviewResponseSchema.safeParse({
        ...bodies.overview,
        channels: [{ ...summary, stats: validStats }],
      }).success,
    ).toBe(true);

    expect(
      stockConnectOverviewResponseSchema.safeParse({
        ...bodies.overview,
        channels: [
          {
            ...summary,
            stats: { ...summary.stats, turnoverAmount: reportedMoneyFact("-1.00") },
          },
        ],
      }).success,
    ).toBe(false);

    expect(
      stockConnectOverviewResponseSchema.safeParse({
        ...bodies.overview,
        channels: [
          {
            ...summary,
            stats: {
              ...validStats,
              turnoverAmount: reportedMoneyFact("99.99"),
              netBuyAmount: derivedMoneyFact("-19.99"),
            },
          },
        ],
      }).success,
    ).toBe(false);

    expect(
      stockConnectOverviewResponseSchema.safeParse({
        ...bodies.overview,
        channels: [
          {
            ...summary,
            stats: { ...summary.stats, netBuyAmount: reportedMoneyFact("-20.00") },
          },
        ],
      }).success,
    ).toBe(false);
  });

  /** 当前交易日历史行版本必须锚定外层 publication，错误版本不得进入页面。 */
  it("rejects trend and security rows with a mismatched current publication version", async () => {
    const bodies = responseBodies();
    const trendPoint = bodies.overview.trend[0];
    const securityActivity = bodies.security.activities[0];
    if (trendPoint === undefined || securityActivity === undefined) {
      throw new Error("合同测试必须包含趋势与证券活动行。");
    }
    const invalidOverview = {
      ...bodies.overview,
      trend: [{ ...trendPoint, dataVersion: "wrong-version" }],
    };
    const invalidSecurity = {
      ...bodies.security,
      activities: [{ ...securityActivity, dataVersion: "wrong-version" }],
    };

    /** 根据端点返回当前交易日版本被篡改的趋势或证券活动。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }
      if (path.endsWith("/overview/query")) {
        return stockConnectResponse(invalidOverview);
      }

      return stockConnectResponse(invalidSecurity);
    });
    await establishSession();
    const date = {
      mode: "EXACT" as const,
      exactDate: contractIdentifiers.tradeDate,
    };

    await expect(
      queryStockConnectOverview({
        date,
        channels: ["SH_NORTHBOUND"],
        trendTradingDays: 20,
      }),
    ).rejects.toMatchObject({
      status: 502,
      code: "stock-connect-response-scope-mismatch",
    });
    await expect(
      queryStockConnectSecurityContext({
        instrumentEntityRef: contractIdentifiers.instrument,
        date,
        channel: null,
        historyTradingDays: 20,
      }),
    ).rejects.toMatchObject({
      status: 502,
      code: "stock-connect-response-scope-mismatch",
    });
  });

  /** 多通道共同 publication 同日版本一致；不同交易日可拥有各自不可变版本。 */
  it("accepts common same-day versions and distinct versions across trade dates", async () => {
    const bodies = responseBodies();
    const firstChannel = bodies.overview.channels[0];
    const currentPoint = bodies.overview.trend[0];
    if (firstChannel === undefined || currentPoint === undefined) {
      throw new Error("合同测试必须包含通道与趋势行。");
    }
    const previousTradeDate = "2026-07-29";
    const aggregateOverview = {
      ...bodies.overview,
      channels: [
        firstChannel,
        {
          ...firstChannel,
          channel: "SZ_NORTHBOUND",
          route: "SHENZHEN",
        },
      ],
      trend: [
        {
          ...currentPoint,
          tradeDate: previousTradeDate,
          dataVersion: "overview-v0",
        },
        {
          ...currentPoint,
          channel: "SZ_NORTHBOUND",
          tradeDate: previousTradeDate,
          dataVersion: "overview-v0",
        },
        currentPoint,
        {
          ...currentPoint,
          channel: "SZ_NORTHBOUND",
        },
      ],
    };

    /** 认证后返回跨两个通道、两个共同交易日的正式 overview 趋势。 */
    setHttpTransportForTests(async (request) => {
      const path = new URL(request.url, "http://quant.local").pathname;
      if (path === "/api/v1/auth/login") {
        return jsonResponse({
          accessToken: "access-contract",
          accessTokenExpiresIn: 600,
          user: currentUserPayload(),
        });
      }
      if (path === "/api/v1/users/me") {
        return jsonResponse(currentUserPayload());
      }

      return stockConnectResponse(aggregateOverview);
    });
    await establishSession();

    await expect(
      queryStockConnectOverview({
        date: { mode: "EXACT", exactDate: contractIdentifiers.tradeDate },
        channels: ["SH_NORTHBOUND", "SZ_NORTHBOUND"],
        trendTradingDays: 20,
      }),
    ).resolves.toMatchObject({
      data: {
        trend: [
          { tradeDate: previousTradeDate, dataVersion: "overview-v0" },
          { tradeDate: previousTradeDate, dataVersion: "overview-v0" },
          { tradeDate: contractIdentifiers.tradeDate, dataVersion: contractIdentifiers.version },
          { tradeDate: contractIdentifiers.tradeDate, dataVersion: contractIdentifiers.version },
        ],
      },
    });
  });
});

/** 按 readiness 合同规则递归排序对象键并保留数组顺序与 Unicode。 */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new TypeError("测试 readiness 含非 JSON 值。");
    return encoded;
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`)
    .join(",")}}`;
}
