/* eslint-disable @typescript-eslint/require-await */

import { HttpStatus } from '@nestjs/common';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../config/app-config.service.js';
import {
  DataOperationsClient,
  DataOperationsInternalError,
} from '../clients/data-operations.client.js';

/** 提供所有内部 data-sync 调用共同需要的最小测试配置。 */
const config = {
  dataSyncInternalBaseUrl: 'http://data-sync-api:8000',
  dataSyncInternalBearerToken: 'test-only-data-sync-internal-bearer-token-000000000000',
  dataSyncInternalReadApiBearerToken: 'test-only-data-sync-read-bearer-token-000000000000000',
  dataSyncInternalOperationsApiBearerToken:
    'test-only-data-sync-operations-bearer-token-000000000000',
  dataSyncInternalRequestTimeoutMs: 5_000,
  dataSyncInternalPreflightTimeoutMs: 310_000,
} as AppConfigService;

/** 覆盖合同 0022 的全部内部 POST 路由与安全错误隔离。 */
describe('DataOperationsClient', () => {
  /** 验证昂贵全窗预检使用独立长预算且网络失败不会自动重复整窗探针。 */
  it('uses the dedicated preflight budget without retrying a full-window probe', async () => {
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout');
    const successFetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({}));
    const successClient = new DataOperationsClient(config, successFetcher);

    await successClient.preflight({}, 'preflight-long-budget');

    expect(timeoutSpy).toHaveBeenLastCalledWith(config.dataSyncInternalPreflightTimeoutMs);
    timeoutSpy.mockRestore();

    const failingFetcher = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError('preflight connection closed'));
    const failingClient = new DataOperationsClient(config, failingFetcher);

    await expect(failingClient.preflight({}, 'preflight-no-retry')).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
      code: 'dependency-unavailable',
    });
    expect(failingFetcher).toHaveBeenCalledTimes(1);
  });

  /** 验证十八条内部路由均通过服务身份 Bearer 和 POST 访问。 */
  it('uses POST for every 0022 internal route', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse({}));
    const client = new DataOperationsClient(config, fetcher);
    const requestId = 'data-operations-client-test';
    const idempotencyKey = 'dataops:00000000-0000-4000-8000-000000000001';

    await client.overview(requestId);
    await client.searchDatasets({}, requestId);
    await client.getDataset({ datasetCode: 'equity.daily' }, requestId);
    await client.preflight({ targets: [] }, requestId);
    await client.submitCommand({}, idempotencyKey, requestId);
    await client.getCommand({ commandId: '00000000-0000-4000-8000-000000000002' }, requestId);
    await client.cancelCommand({}, idempotencyKey, requestId);
    await client.retryCommand({}, idempotencyKey, requestId);
    await client.searchRuns({}, requestId);
    await client.getRun({ runId: '00000000-0000-4000-8000-000000000003' }, requestId);
    await client.searchHealthEvaluations({}, requestId);
    await client.getHealthEvaluation(
      { evaluationId: '00000000-0000-4000-8000-000000000004' },
      requestId,
    );
    await client.submitHealthCheck({}, idempotencyKey, requestId);
    await client.getHealthCheck(
      { healthCheckId: '00000000-0000-4000-8000-000000000005' },
      requestId,
    );
    await client.searchSchedules({}, requestId);
    await client.upsertSchedule({}, idempotencyKey, requestId);
    await client.setScheduleEnabled({}, idempotencyKey, requestId);
    await client.searchEvents({}, requestId);

    expect(fetcher).toHaveBeenCalledTimes(18);
    const paths = fetcher.mock.calls.map(([target]) => requestedPath(target));
    expect(paths).toEqual([
      '/internal/v1/data-operations/overview/query',
      '/internal/v1/data-operations/datasets/search',
      '/internal/v1/data-operations/datasets/detail',
      '/internal/v1/data-operations/commands/preflight',
      '/internal/v1/data-operations/commands/submit',
      '/internal/v1/data-operations/commands/detail',
      '/internal/v1/data-operations/commands/cancel',
      '/internal/v1/data-operations/commands/retry',
      '/internal/v1/data-operations/runs/search',
      '/internal/v1/data-operations/runs/detail',
      '/internal/v1/data-operations/health/evaluations/search',
      '/internal/v1/data-operations/health/evaluations/detail',
      '/internal/v1/data-operations/health/checks/submit',
      '/internal/v1/data-operations/health/checks/detail',
      '/internal/v1/data-operations/schedules/search',
      '/internal/v1/data-operations/schedules/upsert',
      '/internal/v1/data-operations/schedules/set-enabled',
      '/internal/v1/data-operations/events/search',
    ]);
    const mutationIndexes = new Set([4, 6, 7, 12, 15, 16]);
    for (const [index, [, init]] of fetcher.mock.calls.entries()) {
      expect(init?.method).toBe('POST');
      expect(init?.headers).toMatchObject({
        Authorization: `Bearer ${
          mutationIndexes.has(index)
            ? config.dataSyncInternalOperationsApiBearerToken
            : config.dataSyncInternalReadApiBearerToken
        }`,
        'X-Request-Id': requestId,
      });
    }
  });

  /** 验证 dispatcher 专用 deliver 仍通过固定内部 key，而非公开浏览器 key。 */
  it('sends the dispatcher frozen key only to mutation delivery', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse({}));
    const client = new DataOperationsClient(config, fetcher);

    await client.deliver(
      '/internal/v1/data-operations/commands/submit',
      { submissionId: '00000000-0000-4000-8000-000000000001' },
      'dataops:00000000-0000-4000-8000-000000000001',
      'request-delivery',
    );

    expect(fetcher.mock.calls[0]?.[1]?.headers).toMatchObject({
      'Idempotency-Key': 'dataops:00000000-0000-4000-8000-000000000001',
    });
  });

  /** 验证下游 422 保留公开可处理边界，同时 Provider 原文不进入错误对象。 */
  it('maps downstream 422 without exposing its body', () => {
    const client = new DataOperationsClient(config, vi.fn<typeof fetch>());
    const error = new DataOperationsInternalError(
      HttpStatus.UNPROCESSABLE_ENTITY,
      'preflight-rejected',
      undefined,
    );

    expect(() => client.asPublicProblem(error)).toThrow(
      expect.objectContaining({ status: HttpStatus.UNPROCESSABLE_ENTITY }),
    );
  });

  /** 验证可重试只读故障只重试一次，而写投递交给持久 outbox 决定是否重放。 */
  it('retries a read once but does not retry a mutation', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('{}', { status: 503 }))
      .mockResolvedValueOnce(jsonResponse({}))
      .mockResolvedValueOnce(new Response('{}', { status: 503 }));
    const client = new DataOperationsClient(config, fetcher);

    await client.overview('read-retry');
    await expect(
      client.submitCommand({}, 'dataops:retry-boundary', 'write-no-retry'),
    ).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
    });

    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls[0]?.[1]?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalReadApiBearerToken}`,
    });
    expect(fetcher.mock.calls[2]?.[1]?.headers).toMatchObject({
      Authorization: `Bearer ${config.dataSyncInternalOperationsApiBearerToken}`,
    });
  });

  /** 验证连续下游故障达到阈值后，读取断路器在冷却期内不再发起网络请求。 */
  it('opens the read circuit after failures exceed half of twenty requests', async () => {
    const fetcher = vi.fn<typeof fetch>();
    const client = new DataOperationsClient(config, fetcher);
    const internal = client as unknown as {
      recordReadCircuitOutcome: (succeeded: boolean) => void;
    };
    for (let index = 0; index < 20; index += 1) {
      internal.recordReadCircuitOutcome(false);
    }

    await expect(client.overview('circuit-open')).rejects.toMatchObject({
      status: HttpStatus.SERVICE_UNAVAILABLE,
    });
    expect(fetcher).not.toHaveBeenCalled();
  });
});

/** 构造独立 JSON 响应，避免 `Response` body 被前一次调用消费。 */
function jsonResponse(value: Record<string, unknown>): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 将 fetch 接收到的 URL、字符串或 Request 统一转换为 pathname 断言。 */
function requestedPath(target: Parameters<typeof fetch>[0] | undefined): string {
  if (target instanceof URL) return target.pathname;
  if (typeof target === 'string') return new URL(target).pathname;
  if (target instanceof Request) return new URL(target.url).pathname;
  throw new Error('Expected data operations client request target');
}
