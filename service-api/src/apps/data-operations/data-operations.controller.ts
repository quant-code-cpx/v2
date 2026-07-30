import { Body, Controller, Headers, HttpCode, HttpStatus, Post, Req, Res } from '@nestjs/common';
import { ApiBearerAuth, ApiTags } from '@nestjs/swagger';
import type { Response } from 'express';

import { Role } from '../../generated/prisma/client.js';
import { Roles } from '../../common/decorators/roles.decorator.js';
import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import {
  commandActionRequestSchema,
  commandDetailRequestSchema,
  datasetDetailRequestSchema,
  datasetSearchRequestSchema,
  emptyObjectRequestSchema,
  healthCheckDetailRequestSchema,
  healthCheckSubmitRequestSchema,
  healthDetailRequestSchema,
  healthSearchRequestSchema,
  operationSearchRequestSchema,
  runDetailRequestSchema,
  runSearchRequestSchema,
  scheduleEnabledRequestSchema,
  scheduleSearchRequestSchema,
  scheduleUpsertRequestSchema,
  submissionDetailRequestSchema,
  syncPreflightRequestSchema,
  syncSubmitRequestSchema,
} from '../../data-sync/contracts/data-operations.contract.js';
import { DataOperationSubmissionService } from './data-operation-submission.service.js';
import { DataOperationsQueryService } from './data-operations-query.service.js';
import { DataOperationsRateLimitService } from './data-operations-rate-limit.service.js';
import {
  validateDataOperationsRequest,
  validateIdempotencyKey,
} from './data-operations.validation.js';

/** 表示已通过全局 Bearer 鉴权且带请求关联标识的运维请求。 */
type CorrelatedAuthenticatedRequest = AuthenticatedRequest & { requestId: string };

/** 提供 Contract 0023 定义的全部数据运维公开 POST 路由。 */
@ApiTags('data-operations')
@ApiBearerAuth()
@Controller('data-operations')
@Roles(Role.ADMIN, Role.SUPER_ADMIN)
export class DataOperationsController {
  /** 注入权威查询、授权提交和安全限流用例，Controller 不直接调用 data-sync。 */
  public constructor(
    private readonly queries: DataOperationsQueryService,
    private readonly submissions: DataOperationSubmissionService,
    private readonly rateLimit: DataOperationsRateLimitService,
  ) {}

  @Post('overview')
  @HttpCode(HttpStatus.OK)
  /** 返回 data-sync 权威概览与 API 本地可靠交付计数。 */
  public async overview(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    validateDataOperationsRequest(emptyObjectRequestSchema, input);
    return this.noStore(response, this.queries.overview(request.user, request.requestId));
  }

  @Post('datasets/search')
  @HttpCode(HttpStatus.OK)
  /** 返回经过公开投影的数据资产游标页。 */
  public async searchDatasets(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(datasetSearchRequestSchema, input);
    return this.noStore(
      response,
      this.queries.searchDatasets(request.user, parsed, request.requestId),
    );
  }

  @Post('datasets/detail')
  @HttpCode(HttpStatus.OK)
  /** 返回一个数据集的来源、能力、发布与健康详情。 */
  public async getDataset(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(datasetDetailRequestSchema, input);
    return this.noStore(response, this.queries.getDataset(request.user, parsed, request.requestId));
  }

  @Post('sync/preflight')
  @HttpCode(HttpStatus.OK)
  @Roles(Role.SUPER_ADMIN)
  /** 对同步 target 执行无副作用预检，不创建 Submission 或 command。 */
  public async preflight(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(syncPreflightRequestSchema, input);
    await this.rateLimit.assertPreflightAllowed(request.user.userId);
    return this.noStore(response, this.queries.preflight(request.user, parsed, request.requestId));
  }

  @Post('sync/submit')
  @HttpCode(HttpStatus.ACCEPTED)
  @Roles(Role.SUPER_ADMIN)
  /** 原子保存同步授权意图、outbox 与审计，并固定返回首次 PENDING 收据。 */
  public async submitSync(
    @Body() input: unknown,
    @Headers('idempotency-key') idempotencyKey: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(syncSubmitRequestSchema, input);
    await this.rateLimit.assertWriteAllowed(request.user.userId, 'SYNC_SUBMIT');
    return this.noStore(
      response,
      this.submissions.submit(
        request.user,
        'SYNC_SUBMIT',
        parsed,
        validateIdempotencyKey(idempotencyKey),
        request.requestId,
      ),
    );
  }

  @Post('sync/cancel')
  @HttpCode(HttpStatus.ACCEPTED)
  @Roles(Role.SUPER_ADMIN)
  /** 原子保存 COMMAND 或 RUN 的合作式取消意图，不同步判断目标是否存在。 */
  public async cancelSync(
    @Body() input: unknown,
    @Headers('idempotency-key') idempotencyKey: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(commandActionRequestSchema, input);
    await this.rateLimit.assertWriteAllowed(request.user.userId, 'SYNC_CANCEL');
    return this.noStore(
      response,
      this.submissions.submit(
        request.user,
        'SYNC_CANCEL',
        parsed,
        validateIdempotencyKey(idempotencyKey),
        request.requestId,
      ),
    );
  }

  @Post('sync/retry')
  @HttpCode(HttpStatus.ACCEPTED)
  @Roles(Role.SUPER_ADMIN)
  /** 原子保存 COMMAND 或 RUN 的重试意图，不在请求线程创建新 command。 */
  public async retrySync(
    @Body() input: unknown,
    @Headers('idempotency-key') idempotencyKey: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(commandActionRequestSchema, input);
    await this.rateLimit.assertWriteAllowed(request.user.userId, 'SYNC_RETRY');
    return this.noStore(
      response,
      this.submissions.submit(
        request.user,
        'SYNC_RETRY',
        parsed,
        validateIdempotencyKey(idempotencyKey),
        request.requestId,
      ),
    );
  }

  @Post('commands/detail')
  @HttpCode(HttpStatus.OK)
  /** 返回权威命令状态及按提交顺序排列的 child runs。 */
  public async getCommand(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(commandDetailRequestSchema, input);
    return this.noStore(response, this.queries.getCommand(request.user, parsed, request.requestId));
  }

  @Post('runs/search')
  @HttpCode(HttpStatus.OK)
  /** 返回全局串行队列与历史运行的公开游标页。 */
  public async searchRuns(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(runSearchRequestSchema, input);
    return this.noStore(response, this.queries.searchRuns(request.user, parsed, request.requestId));
  }

  @Post('runs/detail')
  @HttpCode(HttpStatus.OK)
  /** 返回运行详情及独立 cursor 的分区、时间线安全投影。 */
  public async getRun(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(runDetailRequestSchema, input);
    return this.noStore(response, this.queries.getRun(request.user, parsed, request.requestId));
  }

  @Post('health/evaluations/search')
  @HttpCode(HttpStatus.OK)
  /** 返回不含规则结果的不可变健康评估摘要页。 */
  public async searchHealthEvaluations(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(healthSearchRequestSchema, input);
    return this.noStore(
      response,
      this.queries.searchHealthEvaluations(request.user, parsed, request.requestId),
    );
  }

  @Post('health/evaluations/detail')
  @HttpCode(HttpStatus.OK)
  /** 返回不可变评估事实与当前 issue 投影，二者不相互改写。 */
  public async getHealthEvaluation(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(healthDetailRequestSchema, input);
    return this.noStore(
      response,
      this.queries.getHealthEvaluation(request.user, parsed, request.requestId),
    );
  }

  @Post('health/checks/submit')
  @HttpCode(HttpStatus.ACCEPTED)
  @Roles(Role.SUPER_ADMIN)
  /** 原子保存主动健康检查授权意图，首次响应不宣称下游已受理。 */
  public async submitHealthCheck(
    @Body() input: unknown,
    @Headers('idempotency-key') idempotencyKey: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(healthCheckSubmitRequestSchema, input);
    await this.rateLimit.assertWriteAllowed(request.user.userId, 'HEALTH_CHECK_SUBMIT');
    return this.noStore(
      response,
      this.submissions.submit(
        request.user,
        'HEALTH_CHECK_SUBMIT',
        parsed,
        validateIdempotencyKey(idempotencyKey),
        request.requestId,
      ),
    );
  }

  @Post('health/checks/detail')
  @HttpCode(HttpStatus.OK)
  /** 返回保持原 target 顺序的主动健康检查详情。 */
  public async getHealthCheck(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(healthCheckDetailRequestSchema, input);
    return this.noStore(
      response,
      this.queries.getHealthCheck(request.user, parsed, request.requestId),
    );
  }

  @Post('schedules/search')
  @HttpCode(HttpStatus.OK)
  /** 返回 data-sync 权威动态计划及其更新主体投影。 */
  public async searchSchedules(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<Record<string, unknown>> {
    const parsed = validateDataOperationsRequest(scheduleSearchRequestSchema, input);
    return this.noStore(
      response,
      this.queries.searchSchedules(request.user, parsed, request.requestId),
    );
  }

  @Post('schedules/upsert')
  @HttpCode(HttpStatus.ACCEPTED)
  @Roles(Role.SUPER_ADMIN)
  /** 原子保存计划创建或乐观锁更新意图，真实冲突由 data-sync 异步裁决。 */
  public async upsertSchedule(
    @Body() input: unknown,
    @Headers('idempotency-key') idempotencyKey: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(scheduleUpsertRequestSchema, input);
    await this.rateLimit.assertWriteAllowed(request.user.userId, 'SCHEDULE_UPSERT');
    return this.noStore(
      response,
      this.submissions.submit(
        request.user,
        'SCHEDULE_UPSERT',
        parsed,
        validateIdempotencyKey(idempotencyKey),
        request.requestId,
      ),
    );
  }

  @Post('schedules/set-enabled')
  @HttpCode(HttpStatus.ACCEPTED)
  @Roles(Role.SUPER_ADMIN)
  /** 原子保存计划启停意图，未知计划由 data-sync 异步拒绝。 */
  public async setScheduleEnabled(
    @Body() input: unknown,
    @Headers('idempotency-key') idempotencyKey: string | undefined,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(scheduleEnabledRequestSchema, input);
    await this.rateLimit.assertWriteAllowed(request.user.userId, 'SCHEDULE_SET_ENABLED');
    return this.noStore(
      response,
      this.submissions.submit(
        request.user,
        'SCHEDULE_SET_ENABLED',
        parsed,
        validateIdempotencyKey(idempotencyKey),
        request.requestId,
      ),
    );
  }

  @Post('submissions/detail')
  @HttpCode(HttpStatus.OK)
  /** 返回本地授权和交付状态机收据，不暴露 outbox lease 或内部 key。 */
  public async getSubmission(
    @Body() input: unknown,
    @Res({ passthrough: true }) response: Response,
  ) {
    const parsed = validateDataOperationsRequest(submissionDetailRequestSchema, input);
    return this.noStore(response, this.submissions.getReceipt(parsed.submissionId));
  }

  @Post('operations/search')
  @HttpCode(HttpStatus.OK)
  /** 返回用户 Submission 与系统 data-sync 事件的窄化合并投影。 */
  public async searchOperations(
    @Body() input: unknown,
    @Req() request: CorrelatedAuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<{ items: unknown[]; nextCursor: string | null }> {
    const parsed = validateDataOperationsRequest(operationSearchRequestSchema, input);
    return this.noStore(
      response,
      this.queries.searchOperations(request.user, parsed, request.requestId),
    );
  }

  /** 为全部成功响应统一设置 no-store，并透传异步用例结果。 */
  private async noStore<T>(response: Response, result: Promise<T>): Promise<T> {
    response.setHeader('Cache-Control', 'no-store');
    return result;
  }
}
