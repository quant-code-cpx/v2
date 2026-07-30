import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const repositoryRoot = new URL("../..", import.meta.url);
const etfDatasets = [
  "fund.etf.profile.reported",
  "fund.etf.bar.1d.reported",
  "fund.etf.nav.1d.reported",
  "fund.etf.trading_state.reported",
];

/** 读取仓库源码或版本化合同，避免架构测试依赖调用时工作目录。 */
async function readRepositoryFile(relativePath) {
  return readFile(new URL(relativePath, repositoryRoot), "utf8");
}

/** 截取一个数据运维目录项，供逐数据集检查执行与来源不变量。 */
function datasetDefinitionBlock(source, datasetCode) {
  const escapedCode = datasetCode.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  const expression = new RegExp(
    `DatasetDefinition\\(\\s*"${escapedCode}"[\\s\\S]*?\\n\\s*\\),`,
    "u",
  );
  const block = expression.exec(source)?.[0];
  assert.ok(block, `数据运维目录缺少 ${datasetCode}`);
  return block;
}

/** typed market-data 根合同必须冻结 ETF v2 文本运算符和完整业务字段。 */
test("ETF v2 根合同覆盖文本运算符与完整公开字段", async () => {
  const contract = await readRepositoryFile("docs/contracts/data-sync-market-data-v1.yaml");

  for (const operator of ["PREFIX", "CONTAINS"]) {
    assert.match(contract, new RegExp(`- ${operator}`, "u"));
  }
  for (const field of [
    "displayName",
    "listingStatus",
    "open",
    "amount",
    "volumeUnit",
    "currency",
    "finality",
    "effectiveTo",
  ]) {
    assert.match(contract, new RegExp(`\\b${field}:`, "u"));
  }
});

/** 同步服务必须从统一控制面执行真实来源，并把四条新 publication 固定到 schema v2。 */
test("service-data-sync 注册 ETF canonical executor、真实来源和 schema v2", async () => {
  const [controlPlane, executors, typedSupport, etfRepository, internalApi] = await Promise.all([
    readRepositoryFile(
      "service-data-sync/src/service_data_sync/infrastructure/data_operations/control_plane.py",
    ),
    readRepositoryFile(
      "service-data-sync/src/service_data_sync/infrastructure/data_operations/canonical_executors.py",
    ),
    readRepositoryFile(
      "service-data-sync/src/service_data_sync/infrastructure/persistence/typed_p0_support.py",
    ),
    readRepositoryFile(
      "service-data-sync/src/service_data_sync/infrastructure/persistence/etf_market_data_repository.py",
    ),
    readRepositoryFile(
      "service-data-sync/src/service_data_sync/interfaces/internal_market_data_api.py",
    ),
  ]);

  for (const datasetCode of etfDatasets) {
    const definition = datasetDefinitionBlock(controlPlane, datasetCode);
    assert.match(definition, /dispatcher_ready=True/u);
    assert.ok(executors.includes(`"${datasetCode}"`), `执行器缺少 ${datasetCode}`);
  }
  assert.match(controlPlane, /sse-szse\.official-etf-directory/u);
  assert.match(controlPlane, /tencent\.etf-kline/u);
  assert.match(controlPlane, /eastmoney\.etf\.nav-json/u);
  assert.doesNotMatch(controlPlane, /ths\.etf-category|eastmoney\.etf\.kline/u);
  assert.match(executors, /for dataset_code in _ETF_EXECUTIONS/u);
  assert.match(typedSupport, /CanonicalDataset\.schema_version == schema_version/u);
  assert.match(etfRepository, /schema_version=2/u);
  assert.match(internalApi, /if isinstance\(value, Decimal\):\s+return format\(value, "f"\)/u);
});

/** API 必须保持 POST-only、严格 ETF v2 白名单、无缓存和有界下游 JSON。 */
test("service-api 以安全 POST 边界公开 ETF typed market-data", async () => {
  const [controller, client, contract] = await Promise.all([
    readRepositoryFile("service-api/src/apps/market/market-data-access.controller.ts"),
    readRepositoryFile("service-api/src/data-sync/clients/market-data-access.client.ts"),
    readRepositoryFile("service-api/src/data-sync/contracts/market-data-access.contract.ts"),
  ]);

  assert.match(controller, /@Post\('query'\)/u);
  assert.doesNotMatch(controller, /@(Get|Put|Patch|Delete|Head|Options)\(/u);
  assert.match(controller, /private, no-store/u);
  assert.match(client, /MAX_RESPONSE_BYTES/u);
  assert.match(client, /2 \* 1024 \* 1024|2_097_152/u);
  assert.match(client, /content-type/u);
  assert.match(client, /content-length/u);
  assert.doesNotMatch(client, /response\.json\(\)/u);
  assert.match(contract, /displayName: z\.string\(\)\.trim\(\)\.min\(1\)\.max\(160\)/u);
  assert.match(contract, /schemaVersion !== 2/u);
  for (const datasetCode of etfDatasets) {
    assert.ok(contract.includes(`'${datasetCode}'`), `API 合同缺少 ${datasetCode}`);
  }
});

/** Web 必须只经共享 API 读取真实 v2 records，并保持路由、独立状态和图表边界。 */
test("service-web ETF 页面贯穿四数据集且不接入生产 fixture", async () => {
  const [router, api, detail, session] = await Promise.all([
    readRepositoryFile("service-web/src/router/index.tsx"),
    readRepositoryFile("service-web/src/api/etfs.ts"),
    readRepositoryFile("service-web/src/views/EtfDetailView/EtfDetailView.tsx"),
    readRepositoryFile("service-web/src/api/auth-session.ts"),
  ]);

  for (const route of [
    "market/funds",
    "market/etfs",
    "market/etfs/:exchange/:symbol",
  ]) {
    assert.ok(router.includes(`path: "${route}"`), `Web 路由缺少 ${route}`);
  }
  assert.match(api, /const marketDataQueryPath = "\/api\/v1\/market-data\/query"/u);
  assert.match(api, /schemaVersion: typeof etfSchemaVersion/u);
  assert.match(api, /displayName: z\.string\(\)\.trim\(\)\.min\(1\)\.max\(160\)/u);
  assert.match(api, /field: "effectiveFrom", direction: "DESC"/u);
  assert.match(api, /page: \{ limit: 500 \}/u);
  assert.doesNotMatch(api, /from ["'][^"']*mocks\//u);
  for (const datasetCode of etfDatasets) {
    assert.ok(api.includes(`"${datasetCode}"`), `Web adapter 缺少 ${datasetCode}`);
  }
  assert.match(detail, /<KlinePanel/u);
  assert.match(detail, /<EtfNavPriceChart/u);
  assert.match(detail, /不计算折溢价/u);
  assert.match(detail, /最近报告/u);
  assert.match(session, /query\.queryKey\[0\] === "market-data"/u);
});
