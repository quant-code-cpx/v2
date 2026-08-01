import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const repositoryRoot = new URL("../..", import.meta.url);

/** 读取仓库内版本化 OpenAPI 合同，避免测试依赖工作目录。 */
async function readContract(relativePath) {
  return readFile(new URL(relativePath, repositoryRoot), "utf8");
}

/** 读取实现或编排源码，验证版本化合同确实贯穿三项服务。 */
async function readRepositorySource(relativePath) {
  return readFile(new URL(relativePath, repositoryRoot), "utf8");
}

/** 提取顶层 Compose 服务块，避免健康检查断言误命中相邻服务。 */
function composeServiceBlock(document, serviceName) {
  const escapedName = serviceName.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  const expression = new RegExp(
    `^  ${escapedName}:\\n(?<body>[\\s\\S]*?)(?=^  [a-z][a-z0-9-]*:\\n|^networks:|^volumes:|(?![\\s\\S]))`,
    "mu",
  );
  const match = expression.exec(document);
  assert.ok(match?.groups?.body, `编排缺少服务 ${serviceName}`);
  return match.groups.body;
}

/** 从当前合同格式中提取每条路径唯一的 HTTP 方法和 operationId。 */
function parseOperations(document) {
  const operations = [];
  let currentPath;
  let currentMethod;

  for (const line of document.split(/\r?\n/u)) {
    const pathMatch = /^  (\/[^:]+):$/.exec(line);
    if (pathMatch) {
      currentPath = pathMatch[1];
      currentMethod = undefined;
      continue;
    }

    const methodMatch = /^    ([a-z]+):$/.exec(line);
    if (currentPath && methodMatch) {
      currentMethod = methodMatch[1];
      operations.push({ path: currentPath, method: currentMethod, operationId: undefined });
      continue;
    }

    const operationIdMatch = /^      operationId: (\w+)$/.exec(line);
    if (currentPath && currentMethod && operationIdMatch) {
      operations.at(-1).operationId = operationIdMatch[1];
    }
  }

  return operations;
}

/** 取得指定路径的 YAML 块，供字段级合同不变量断言使用。 */
function pathBlock(document, path) {
  const escapedPath = path.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  const expression = new RegExp(
    `^  ${escapedPath}:\\n(?<body>[\\s\\S]*?)(?=^  /|^components:)`,
    "mu",
  );
  const match = expression.exec(document);
  assert.ok(match?.groups?.body, `合同缺少路径 ${path}`);
  return match.groups.body;
}

/** 取得组件 schema 的 YAML 块，供跨入口目标语义断言使用。 */
function schemaBlock(document, schemaName) {
  const escapedName = schemaName.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  const schemasStart = document.indexOf("  schemas:\n");
  assert.notEqual(schemasStart, -1, "合同缺少 components.schemas");
  const afterSchemas = document.slice(schemasStart + "  schemas:\n".length);
  const nextComponentSection = afterSchemas.search(/^  [a-z][A-Za-z0-9]*:\n/mu);
  const schemas =
    nextComponentSection === -1 ? afterSchemas : afterSchemas.slice(0, nextComponentSection);
  const expression = new RegExp(
    `^    ${escapedName}:\\n(?<body>[\\s\\S]*?)(?=^    [A-Z][A-Za-z0-9]+:|(?![\\s\\S]))`,
    "mu",
  );
  const match = expression.exec(schemas);
  assert.ok(match?.groups?.body, `合同缺少 schema ${schemaName}`);
  return match.groups.body;
}

const internalExpected = [
  ["/internal/v1/data-operations/overview/query", "queryDataOperationsOverviewInternal"],
  ["/internal/v1/data-operations/datasets/search", "searchOperationalDatasetsInternal"],
  ["/internal/v1/data-operations/datasets/detail", "getOperationalDatasetInternal"],
  ["/internal/v1/data-operations/commands/preflight", "preflightDataSyncCommandInternal"],
  ["/internal/v1/data-operations/commands/submit", "submitDataSyncCommandInternal"],
  ["/internal/v1/data-operations/commands/detail", "getDataSyncCommandInternal"],
  ["/internal/v1/data-operations/commands/cancel", "cancelDataSyncCommandInternal"],
  ["/internal/v1/data-operations/commands/retry", "retryDataSyncCommandInternal"],
  ["/internal/v1/data-operations/runs/search", "searchDataSyncRunsInternal"],
  ["/internal/v1/data-operations/runs/detail", "getDataSyncRunInternal"],
  [
    "/internal/v1/data-operations/health/evaluations/search",
    "searchDatasetHealthEvaluationsInternal",
  ],
  [
    "/internal/v1/data-operations/health/evaluations/detail",
    "getDatasetHealthEvaluationInternal",
  ],
  [
    "/internal/v1/data-operations/health/checks/submit",
    "submitDatasetHealthCheckInternal",
  ],
  [
    "/internal/v1/data-operations/health/checks/detail",
    "getDatasetHealthCheckInternal",
  ],
  ["/internal/v1/data-operations/schedules/search", "searchDataSyncSchedulesInternal"],
  ["/internal/v1/data-operations/schedules/upsert", "upsertDataSyncScheduleInternal"],
  [
    "/internal/v1/data-operations/schedules/set-enabled",
    "setDataSyncScheduleEnabledInternal",
  ],
  ["/internal/v1/data-operations/events/search", "searchDataOperationEventsInternal"],
];

const publicExpected = [
  ["/data-operations/overview", "queryDataOperationsOverview"],
  ["/data-operations/datasets/search", "searchOperationalDatasets"],
  ["/data-operations/datasets/detail", "getOperationalDataset"],
  ["/data-operations/sync/preflight", "preflightDataSync"],
  ["/data-operations/sync/submit", "submitDataSync"],
  ["/data-operations/sync/cancel", "cancelDataSync"],
  ["/data-operations/sync/retry", "retryDataSync"],
  ["/data-operations/commands/detail", "getDataSyncCommand"],
  ["/data-operations/runs/search", "searchDataSyncRuns"],
  ["/data-operations/runs/detail", "getDataSyncRun"],
  ["/data-operations/health/evaluations/search", "searchDatasetHealthEvaluations"],
  ["/data-operations/health/evaluations/detail", "getDatasetHealthEvaluation"],
  ["/data-operations/health/checks/submit", "submitDatasetHealthCheck"],
  ["/data-operations/health/checks/detail", "getDatasetHealthCheck"],
  ["/data-operations/schedules/search", "searchDataSyncSchedules"],
  ["/data-operations/schedules/upsert", "upsertDataSyncSchedule"],
  ["/data-operations/schedules/set-enabled", "setDataSyncScheduleEnabled"],
  ["/data-operations/submissions/detail", "getDataOperationSubmission"],
  ["/data-operations/operations/search", "searchDataOperations"],
];

/** 验证内部 18 条路由保留 POST-only 与确定的 operationId。 */
test("0022 数据运维内部合同保持 18 条 POST 路由", async () => {
  const document = await readContract("docs/contracts/0022-data-sync-operations-internal.openapi.yaml");
  const operations = parseOperations(document);

  assert.equal(operations.length, internalExpected.length);
  assert.ok(operations.every((operation) => operation.method === "post"));
  assert.deepEqual(
    operations.map((operation) => [operation.path, operation.operationId]),
    internalExpected,
  );
});

/** 验证公开 19 条路由保留 POST-only 与确定的 operationId。 */
test("0023 数据运维公开合同保持 19 条 POST 路由", async () => {
  const document = await readContract("docs/contracts/0023-service-api-data-operations.openapi.yaml");
  const operations = parseOperations(document);

  assert.equal(operations.length, publicExpected.length);
  assert.ok(operations.every((operation) => operation.method === "post"));
  assert.deepEqual(
    operations.map((operation) => [operation.path, operation.operationId]),
    publicExpected,
  );
});

/** 验证全部公开写意图都具备稳定幂等键和异步 202 受理语义。 */
test("0023 公开写动作统一使用 Idempotency-Key 与 Submission 回执", async () => {
  const document = await readContract("docs/contracts/0023-service-api-data-operations.openapi.yaml");
  const mutationPaths = [
    "/data-operations/sync/submit",
    "/data-operations/sync/cancel",
    "/data-operations/sync/retry",
    "/data-operations/health/checks/submit",
    "/data-operations/schedules/upsert",
    "/data-operations/schedules/set-enabled",
  ];

  for (const path of mutationPaths) {
    const block = pathBlock(document, path);
    assert.match(block, /- \$ref: "#\/components\/parameters\/IdempotencyKey"/u);
    assert.match(block, /"202":\n          \$ref: "#\/components\/responses\/Submission"/u);
  }
});

/** 验证既有生产入口所需的 selector 已在内外合同统一且不可退化为任意 JSON。 */
test("同步 target 与计划使用严格 selector，覆盖既有生产目标范围", async () => {
  const [internal, external, webTypes] = await Promise.all([
    readContract("docs/contracts/0022-data-sync-operations-internal.openapi.yaml"),
    readContract("docs/contracts/0023-service-api-data-operations.openapi.yaml"),
    readRepositorySource("service-web/src/types/data-operations.ts"),
  ]);
  const target = schemaBlock(internal, "SyncTarget");
  const selector = schemaBlock(internal, "TargetSelector");
  const capability = schemaBlock(internal, "DatasetCapability");
  const schedule = schemaBlock(internal, "ScheduleUpsertRequest");
  const publicSchedule = schemaBlock(external, "ScheduleUpsertRequest");

  assert.match(target, /required: \[datasetCode, mode, selector\]/u);
  assert.match(capability, /selectorKinds:/u);
  for (const [kind, schemaName] of [
    ["GLOBAL", "GlobalTargetSelector"],
    ["INSTRUMENT", "InstrumentTargetSelector"],
    ["SECTOR", "SectorTargetSelector"],
    ["SCHEME", "SchemeTargetSelector"],
    ["EXCHANGE", "ExchangeTargetSelector"],
    ["CONTRACT", "ContractTargetSelector"],
    ["ETF", "EtfTargetSelector"],
    ["MARGIN", "MarginTargetSelector"],
    ["STOCK_CONNECT", "StockConnectTargetSelector"],
    ["TRADING_EVENT", "TradingEventTargetSelector"],
    ["INDEX", "IndexTargetSelector"],
  ]) {
    assert.match(selector, new RegExp(schemaName, "u"));
    const variant = schemaBlock(internal, schemaName);
    assert.match(variant, new RegExp(`const: ${kind}`, "u"));
    assert.match(variant, /additionalProperties: false/u);
  }
  assert.match(schedule, /selector:/u);
  assert.match(publicSchedule, /selector:/u);
  assert.match(
    schemaBlock(internal, "ScheduleFrequency"),
    /required:\n\s+\[kind, timezone, localTime, dayOfWeek, dayOfMonth, intervalMinutes, calendarCode\]/u,
  );
  assert.match(
    schemaBlock(internal, "DatasetAvailability"),
    /enum: \[ENABLED, DISABLED, SOURCE_UNAVAILABLE, MODEL_ONLY, UNKNOWN\]/u,
  );
  assert.match(
    internal,
    /availability:\n\s+type: array\n\s+maxItems: 5\n\s+items:\n\s+\$ref: "#\/components\/schemas\/DatasetAvailability"/u,
  );
  assert.match(webTypes, /"MODEL_ONLY"/u);
  assert.match(
    schemaBlock(internal, "RunDetail"),
    /attempt:\n\s+type: integer\n\s+minimum: 0/u,
  );
});

/** 验证 data-sync 路由与 API 内部 HTTP client 同步覆盖全部 18 条内部 POST。 */
test("0022 内部路由已由 data-sync 实现且仅经 API HTTP client 调用", async () => {
  const [router, client] = await Promise.all([
    readRepositorySource(
      "service-data-sync/src/service_data_sync/interfaces/internal_data_operations_api.py",
    ),
    readRepositorySource("service-api/src/data-sync/clients/data-operations.client.ts"),
  ]);

  assert.doesNotMatch(router, /@app\.(?:get|put|patch|delete|head|options)\(/u);
  for (const [path] of internalExpected) {
    assert.ok(router.includes(`\"${path}\"`), `data-sync 未实现 ${path}`);
    assert.ok(client.includes(`'${path}'`), `service-api client 未调用 ${path}`);
  }
  assert.match(client, /method: 'POST'/u);
});

/** 验证 API Controller 与 Web adapter 同步覆盖全部 19 条公开 POST。 */
test("0023 公开路由已由 API Controller 和 Web adapter 完整映射", async () => {
  const [controller, adapter] = await Promise.all([
    readRepositorySource("service-api/src/apps/data-operations/data-operations.controller.ts"),
    readRepositorySource("service-web/src/api/data-operations.ts"),
  ]);

  assert.doesNotMatch(controller, /@(Get|Put|Patch|Delete|Head|Options)\(/u);
  assert.match(adapter, /Authorization: `Bearer \$\{accessToken\}`/u);
  for (const [path] of publicExpected) {
    const adapterSuffix = path.replace("/data-operations", "");
    const controllerSuffix = adapterSuffix.slice(1);
    assert.ok(controller.includes(`@Post('${controllerSuffix}')`), `service-api 未实现 ${path}`);
    assert.ok(adapter.includes(`\"${adapterSuffix}\"`), `service-web 未映射 ${path}`);
  }
});

/** 验证每个公开列表资源使用自己的不透明分页 cursor，避免跨端点传递错误令牌。 */
test("Web 为目录、运行、健康、计划和操作记录隔离 cursor", async () => {
  const [hook, urlState, pager] = await Promise.all([
    readRepositorySource(
      "service-web/src/views/DataOperationsView/hooks/useDataOperationsPage.ts",
    ),
    readRepositorySource("service-web/src/views/DataOperationsView/utils/data-operations-url.ts"),
    readRepositorySource("service-web/src/views/DataOperationsView/components/CursorPager.tsx"),
  ]);

  assert.doesNotMatch(hook, /cursor: state\.catalog\.cursor/u);
  for (const cursor of ["runCursor", "healthCursor", "scheduleCursor", "operationCursor"]) {
    assert.match(hook, new RegExp(`cursor: state\\.${cursor}`, "u"));
    assert.match(urlState, new RegExp(`${cursor}`, "u"));
  }
  assert.match(pager, /所属资源返回的 cursor/u);
});

/** 验证可靠 outbox dispatcher 在开发与生产编排中都晚于迁移和内部 API 启动。 */
test("编排启动独立 data operations dispatcher 并等待迁移和 data-sync", async () => {
  const [base, development, production] = await Promise.all([
    readRepositorySource("compose.yaml"),
    readRepositorySource("compose.dev.yaml"),
    readRepositorySource("compose.prod.yaml"),
  ]);

  assert.match(base, /service-api-data-operations-dispatcher:/u);
  assert.match(base, /dist\/scripts\/dispatch-data-operations\.js/u);
  assert.match(base, /service-api-migrate:\n        condition: service_completed_successfully/u);
  assert.match(base, /data-sync-api:\n        condition: service_healthy/u);
  assert.match(development, /service-api-data-operations-dispatcher:/u);
  assert.match(development, /tsx src\/scripts\/dispatch-data-operations\.ts/u);
  assert.match(production, /service-api-data-operations-dispatcher:/u);
});

/** 验证 worker 与 scheduler 仅做无 I/O 存活探测，避免周期诊断堆积阻塞线程。 */
test("data-sync worker 与 scheduler 使用无 I/O 存活健康检查", async () => {
  const base = await readRepositorySource("compose.yaml");
  const worker = composeServiceBlock(base, "data-sync-worker");
  const scheduler = composeServiceBlock(base, "data-sync-scheduler");

  assert.match(worker, /healthcheck:\n(?:      # [^\n]+\n)?      test: \["CMD-SHELL", "kill -0 1"\]/u);
  assert.doesNotMatch(worker, /data-sync-diagnostics/u);
  assert.match(scheduler, /extends:\n      service: data-sync-worker/u);
  assert.doesNotMatch(scheduler, /healthcheck:/u);
});
