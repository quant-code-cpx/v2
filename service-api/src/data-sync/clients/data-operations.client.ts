import { HttpStatus, Injectable } from '@nestjs/common';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { AppConfigService } from '../../config/app-config.service.js';
import {
  internalDataOperationsResponseSchema,
  type InternalDataOperationsResponse,
} from '../contracts/data-operations.contract.js';

/** 表示可在单元测试中替换的标准 Fetch 实现。 */
type FetchLike = typeof fetch;

/** 表示 data-sync 内部服务身份可访问的最小权限范围。 */
type DataOperationsCredentialScope = 'READ' | 'OPERATIONS';

/** 表示只读断路器记录的一次完整逻辑请求结果。 */
type ReadCircuitOutcome = { occurredAt: number; succeeded: boolean };

/** 表示下游内部 API 已安全归一化后的失败，不承载其原始正文。 */
export class DataOperationsInternalError extends Error {
  /** 保存调度器重试和公开错误映射所需的最小稳定信息。 */
  public constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly retryAfter: number | undefined,
  ) {
    super('Data operations internal request failed');
  }
}

/** 通过版本化内部 POST 合同读取与写入数据运维资源，绝不访问同步库或 Provider。 */
@Injectable()
export class DataOperationsClient {
  /** 保存最近只读请求的结果，用于在下游持续故障时快速失败。 */
  private readonly readCircuitOutcomes: ReadCircuitOutcome[] = [];

  /** 保存只读断路器的打开截止时间；零表示闭合。 */
  private readCircuitOpenUntil = 0;

  /** 标记半开窗口中唯一允许的探测请求，避免故障恢复时流量突刺。 */
  private readCircuitProbeInFlight = false;

  /** 注入集中配置与可替换 Fetch，以保持跨服务边界可测试。 */
  public constructor(
    private readonly config: AppConfigService,
    private readonly fetcher: FetchLike = fetch,
  ) {}

  /** 查询 data-sync 权威总览。 */
  public overview(requestId: string): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/overview/query', {}, requestId);
  }

  /** 查询数据资产目录。 */
  public searchDatasets(
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/datasets/search', request, requestId);
  }

  /** 查询一个数据资产详情。 */
  public getDataset(request: unknown, requestId: string): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/datasets/detail', request, requestId);
  }

  /** 读取无副作用同步预检结果。 */
  public preflight(request: unknown, requestId: string): Promise<InternalDataOperationsResponse> {
    return this.readWithRetry(
      '/internal/v1/data-operations/commands/preflight',
      request,
      requestId,
      this.config.dataSyncInternalPreflightTimeoutMs,
      1,
    );
  }

  /** 投递冻结的同步提交请求。 */
  public submitCommand(
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(
      '/internal/v1/data-operations/commands/submit',
      request,
      idempotencyKey,
      requestId,
    );
  }

  /** 查询权威命令详情。 */
  public getCommand(request: unknown, requestId: string): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/commands/detail', request, requestId);
  }

  /** 投递冻结的取消命令请求。 */
  public cancelCommand(
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(
      '/internal/v1/data-operations/commands/cancel',
      request,
      idempotencyKey,
      requestId,
    );
  }

  /** 投递冻结的命令重试请求。 */
  public retryCommand(
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(
      '/internal/v1/data-operations/commands/retry',
      request,
      idempotencyKey,
      requestId,
    );
  }

  /** 查询全局队列与运行历史。 */
  public searchRuns(request: unknown, requestId: string): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/runs/search', request, requestId);
  }

  /** 查询一个运行的独立分区与时间线游标页。 */
  public getRun(request: unknown, requestId: string): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/runs/detail', request, requestId);
  }

  /** 查询不可变健康评估摘要页。 */
  public searchHealthEvaluations(
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/health/evaluations/search', request, requestId);
  }

  /** 查询一个健康评估与当前问题投影。 */
  public getHealthEvaluation(
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/health/evaluations/detail', request, requestId);
  }

  /** 投递冻结的主动健康检查请求。 */
  public submitHealthCheck(
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(
      '/internal/v1/data-operations/health/checks/submit',
      request,
      idempotencyKey,
      requestId,
    );
  }

  /** 查询批量健康检查和原提交顺序 target 结果。 */
  public getHealthCheck(
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/health/checks/detail', request, requestId);
  }

  /** 查询动态计划与其版本。 */
  public searchSchedules(
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/schedules/search', request, requestId);
  }

  /** 投递冻结的计划创建或更新请求。 */
  public upsertSchedule(
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(
      '/internal/v1/data-operations/schedules/upsert',
      request,
      idempotencyKey,
      requestId,
    );
  }

  /** 投递冻结的计划启停请求。 */
  public setScheduleEnabled(
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(
      '/internal/v1/data-operations/schedules/set-enabled',
      request,
      idempotencyKey,
      requestId,
    );
  }

  /** 由 outbox dispatcher 以已冻结路径与内部幂等键投递一次 mutation。 */
  public deliver(
    internalPath: string,
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.mutate(internalPath, request, idempotencyKey, requestId);
  }

  /** 查询 data-sync 追加式运维事件。 */
  public searchEvents(
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.read('/internal/v1/data-operations/events/search', request, requestId);
  }

  /** 将同步读取错误转换为不泄露下游细节的公开 Problem。 */
  public asPublicProblem(error: unknown): never {
    if (!(error instanceof DataOperationsInternalError)) {
      throw dependencyUnavailable();
    }
    if (error.status === 400) {
      throw new PublicProblemException(
        HttpStatus.BAD_REQUEST,
        'validation-error',
        'Data operations request is invalid',
      );
    }
    if (error.status === 422) {
      throw new PublicProblemException(
        HttpStatus.UNPROCESSABLE_ENTITY,
        'unprocessable-content',
        'Data operations request is not supported',
      );
    }
    if (error.status === 404) {
      throw new PublicProblemException(
        HttpStatus.NOT_FOUND,
        'not-found',
        'Data operations resource is not found',
      );
    }
    if (error.status === 409) {
      throw new PublicProblemException(
        HttpStatus.CONFLICT,
        'conflict',
        'Data operations request conflicts with current state',
      );
    }
    if (error.status === 429) {
      throw new PublicProblemException(
        HttpStatus.TOO_MANY_REQUESTS,
        'rate-limited',
        'Data operations dependency is rate limited',
        error.retryAfter,
      );
    }
    throw dependencyUnavailable();
  }

  /** 执行不携带下游幂等键的内部读取 POST。 */
  private read(
    requestPath: string,
    request: unknown,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.readWithRetry(
      requestPath,
      request,
      requestId,
      Math.min(this.config.dataSyncInternalRequestTimeoutMs, 2_000),
      2,
    );
  }

  /** 执行携带冻结内部幂等键的内部 mutation POST。 */
  private mutate(
    requestPath: string,
    request: unknown,
    idempotencyKey: string,
    requestId: string,
  ): Promise<InternalDataOperationsResponse> {
    return this.post(
      requestPath,
      request,
      requestId,
      idempotencyKey,
      'OPERATIONS',
      this.config.dataSyncInternalRequestTimeoutMs,
    );
  }

  /**
   * 执行只读 POST 的一次有限重试与半开断路器策略；写操作绝不在 client 内重试。
   * 重放写操作只能由持久 outbox 使用原下游幂等键完成。
   */
  private async readWithRetry(
    requestPath: string,
    request: unknown,
    requestId: string,
    timeoutMs: number,
    maximumAttempts: 1 | 2,
  ): Promise<InternalDataOperationsResponse> {
    this.enterReadCircuit();
    let lastError: unknown;
    for (let attempt = 0; attempt < maximumAttempts; attempt += 1) {
      try {
        const response = await this.post(
          requestPath,
          request,
          requestId,
          undefined,
          'READ',
          timeoutMs,
        );
        this.recordReadCircuitOutcome(true);
        return response;
      } catch (error: unknown) {
        lastError = error;
        if (attempt + 1 < maximumAttempts && retryableReadError(error)) {
          await pauseForReadRetry();
          continue;
        }
      }
      break;
    }
    this.recordReadCircuitOutcome(false);
    throw lastError;
  }

  /** 发起有最小权限身份、关联标识、超时和最大响应体边界的内部 POST。 */
  private async post(
    requestPath: string,
    request: unknown,
    requestId: string,
    idempotencyKey?: string,
    credentialScope: DataOperationsCredentialScope = 'READ',
    timeoutMs = this.config.dataSyncInternalRequestTimeoutMs,
  ): Promise<InternalDataOperationsResponse> {
    const url = new URL(requestPath, this.config.dataSyncInternalBaseUrl);
    let response: Response;
    try {
      response = await this.fetcher(url, {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${this.bearerToken(credentialScope)}`,
          'Content-Type': 'application/json',
          'X-Request-Id': requestId,
          ...(idempotencyKey === undefined ? {} : { 'Idempotency-Key': idempotencyKey }),
        },
        body: JSON.stringify(request),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch {
      throw new DataOperationsInternalError(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-unavailable',
        undefined,
      );
    }

    if (!response.ok) {
      throw await this.problemFrom(response);
    }

    try {
      const value = await this.readJsonObject(response);
      return internalDataOperationsResponseSchema.parse(value);
    } catch {
      throw new DataOperationsInternalError(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-contract-invalid',
        undefined,
      );
    }
  }

  /** 选择与内部路径访问范围一致的服务身份，避免读凭据可投递写操作。 */
  private bearerToken(scope: DataOperationsCredentialScope): string {
    return scope === 'READ'
      ? this.config.dataSyncInternalReadApiBearerToken
      : this.config.dataSyncInternalOperationsApiBearerToken;
  }

  /** 在只读断路器打开时拒绝普通流量，仅在冷却结束后放行一个半开探测。 */
  private enterReadCircuit(): void {
    const now = Date.now();
    if (this.readCircuitOpenUntil === 0) return;
    if (now < this.readCircuitOpenUntil) {
      throw new DataOperationsInternalError(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-circuit-open',
        undefined,
      );
    }
    if (this.readCircuitProbeInFlight) {
      throw new DataOperationsInternalError(
        HttpStatus.SERVICE_UNAVAILABLE,
        'dependency-circuit-open',
        undefined,
      );
    }
    this.readCircuitProbeInFlight = true;
  }

  /** 记录完整只读请求的结果，并在 20 次中失败过半时打开 30 秒断路器。 */
  private recordReadCircuitOutcome(succeeded: boolean): void {
    const now = Date.now();
    if (this.readCircuitProbeInFlight) {
      this.readCircuitProbeInFlight = false;
      if (succeeded) {
        this.readCircuitOpenUntil = 0;
        this.readCircuitOutcomes.length = 0;
      } else {
        this.readCircuitOpenUntil = now + 30_000;
      }
      return;
    }
    this.readCircuitOutcomes.push({ occurredAt: now, succeeded });
    if (this.readCircuitOutcomes.length > 20) this.readCircuitOutcomes.shift();
    if (this.readCircuitOutcomes.length !== 20) return;
    const failures = this.readCircuitOutcomes.filter((outcome) => !outcome.succeeded).length;
    if (failures * 2 >= this.readCircuitOutcomes.length) {
      this.readCircuitOpenUntil = now + 30_000;
    }
  }

  /** 从下游 Problem 中仅提取稳定代码和受限 Retry-After，绝不保留正文。 */
  private async problemFrom(response: Response): Promise<DataOperationsInternalError> {
    let code = 'dependency-request-failed';
    try {
      const value = await this.readJsonObject(response);
      const candidate = value.code;
      if (typeof candidate === 'string' && /^[a-z][a-z0-9-]{0,79}$/.test(candidate)) {
        code = candidate;
      }
    } catch {
      // 无法解析的下游正文仍只映射为稳定通用错误。
    }
    return new DataOperationsInternalError(response.status, code, retryAfter(response));
  }

  /** 读取受 2 MiB 上限保护的 JSON 对象，防止下游异常响应耗尽 API 内存。 */
  private async readJsonObject(response: Response): Promise<Record<string, unknown>> {
    const headerLength = response.headers.get('content-length');
    const declaredLength = headerLength === null ? undefined : Number(headerLength);
    if (
      declaredLength !== undefined &&
      (!Number.isSafeInteger(declaredLength) || declaredLength > 2 * 1024 * 1024)
    ) {
      throw new Error('response too large');
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > 2 * 1024 * 1024) {
      throw new Error('response too large');
    }
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      throw new Error('response is not an object');
    }
    return parsed as Record<string, unknown>;
  }
}

/** 将网络、内部身份、响应大小或合同漂移收敛为安全 503。 */
function dependencyUnavailable(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.SERVICE_UNAVAILABLE,
    'dependency-unavailable',
    'Data operations are temporarily unavailable',
  );
}

/** 解析受合同约束的 Retry-After 秒数，其他值一律忽略。 */
function retryAfter(response: Response): number | undefined {
  const value = Number(response.headers.get('retry-after'));
  return Number.isSafeInteger(value) && value >= 1 && value <= 300 ? value : undefined;
}

/** 判断只读请求是否可以在总时限内安全重试一次；写操作永不复用此判断。 */
function retryableReadError(error: unknown): boolean {
  return (
    error instanceof DataOperationsInternalError && [429, 502, 503, 504].includes(error.status)
  );
}

/** 在极短随机退避后重试只读请求，避免多实例同时向刚恢复的下游突发请求。 */
function pauseForReadRetry(): Promise<void> {
  const delayMs = 25 + Math.floor(Math.random() * 76);
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}
