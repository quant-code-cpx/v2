import { Injectable } from '@nestjs/common';

import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import {
  EquityWorkspaceClient,
  type EquityWorkspaceConditionalRead,
} from '../../data-sync/clients/equity-workspace.client.js';
import type {
  EquityDataStatusResponse,
  EquityEventResponse,
  EquitySearchResponse,
  InternalEquitySearchRequest,
} from '../../data-sync/contracts/equity-workspace.contract.js';
import {
  equityDataStatusRequestSchema,
  equityEventSearchRequestSchema,
  equitySearchRequestSchema,
  parseEquityWorkspaceRequest,
  type EquityEventSearchRequestDto,
  type EquitySearchRequestDto,
} from './dto/equity-workspace.dto.js';
import type { EquityPathDto } from './dto/equity-path.dto.js';
import { EquityWorkspaceRateLimitService } from './equity-workspace-rate-limit.service.js';

/** 编排股票中心请求校验、读取限流和同步服务防腐调用。 */
@Injectable()
export class EquityWorkspaceService {
  /** 注入唯一市场事实访问边界和短期防滥用控制。 */
  public constructor(
    private readonly client: EquityWorkspaceClient,
    private readonly rateLimit: EquityWorkspaceRateLimitService,
  ) {}

  /** 执行全市场证券发现，不跨证券扇出读取详情数据集。 */
  public async search(
    input: unknown,
    ifNoneMatch: string | undefined,
    requestId: string,
    actorId: string,
  ): Promise<EquityWorkspaceConditionalRead<EquitySearchResponse>> {
    const parsedRequest = parseEquityWorkspaceRequest(equitySearchRequestSchema, input);
    // 默认只查询已上市证券，避免把无已验证 producer 的暂停上市枚举当作已有市场覆盖。
    const request: EquitySearchRequestDto = {
      ...parsedRequest,
      listingStatuses: parsedRequest.listingStatuses ?? ['LISTED'],
    };
    assertIfNoneMatch(ifNoneMatch);
    assertSearchSets(request);
    assertSearchCapabilities(request);
    await this.rateLimit.assertSearchAllowed(actorId, request);
    return this.client.search({
      body: internalSearchRequest(request),
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取一只证券的统一事件流，并限制日期跨度和未来知识时间。 */
  public async searchEvents(
    path: EquityPathDto,
    input: unknown,
    ifNoneMatch: string | undefined,
    requestId: string,
    actorId: string,
  ): Promise<EquityWorkspaceConditionalRead<EquityEventResponse>> {
    const request = parseEquityWorkspaceRequest(equityEventSearchRequestSchema, input);
    assertIfNoneMatch(ifNoneMatch);
    assertEventRequest(request);
    await this.rateLimit.assertEventsAllowed(actorId);
    return this.client.searchEvents({
      exchange: path.exchange,
      symbol: path.symbol,
      body: request,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取详情页各数据集独立状态，不读取或聚合事实记录。 */
  public async getDataStatus(
    path: EquityPathDto,
    input: unknown,
    ifNoneMatch: string | undefined,
    requestId: string,
    actorId: string,
  ): Promise<EquityWorkspaceConditionalRead<EquityDataStatusResponse>> {
    const request = parseEquityWorkspaceRequest(equityDataStatusRequestSchema, input);
    assertIfNoneMatch(ifNoneMatch);
    assertKnownAt(request.knownAt);
    assertUnique(request.families);
    await this.rateLimit.assertDataStatusAllowed(actorId);
    return this.client.getDataStatus({
      exchange: path.exchange,
      symbol: path.symbol,
      body: request,
      ifNoneMatch,
      requestId,
    });
  }
}

/** 把公开 listingStatuses 改为内部 lifecycleStatuses，其余字段保持冻结名称。 */
function internalSearchRequest(input: EquitySearchRequestDto): InternalEquitySearchRequest {
  const { listingStatuses, ...shared } = input;
  return {
    ...shared,
    ...(listingStatuses === undefined ? {} : { lifecycleStatuses: listingStatuses }),
  };
}

/** 校验搜索数组去重，防止同一谓词重复放大下游查询。 */
function assertSearchSets(input: EquitySearchRequestDto): void {
  assertUnique(input.exchanges);
  assertUnique(input.listingStatuses);
  assertUnique(input.tradingStatuses);
  assertUnique(input.columns);
  assertUnique(input.memberships?.map(membershipKey));
  assertUnique(input.sort?.map(sortField));
}

/** 对没有已验证 producer 或 publication 的筛选失败关闭，避免把未覆盖误读为合法空集。 */
function assertSearchCapabilities(input: EquitySearchRequestDto): void {
  if (input.listingStatuses?.includes('SUSPENDED') === true) {
    throw new PublicProblemException(
      409,
      'capability-unavailable',
      'Suspended listing lifecycle discovery is not available',
    );
  }
  const requestsMoneyFlow =
    input.moneyFlow !== undefined ||
    input.columns?.some(
      /** 资金流列只有 validated production publication 存在后才能开放。 */
      (column) => column === 'moneyFlowNetAmount' || column === 'moneyFlowNetRatio',
    ) === true ||
    input.sort?.some(
      /** 当前唯一资金流排序字段同样必须失败关闭。 */
      (sort) => sort.field === 'moneyFlowNetAmount',
    ) === true;
  if (requestsMoneyFlow) {
    throw new PublicProblemException(
      409,
      'capability-unavailable',
      'Equity money-flow discovery is not available',
    );
  }
}

/** 生成分类筛选去重键。 */
function membershipKey(input: NonNullable<EquitySearchRequestDto['memberships']>[number]): string {
  return `${input.scheme}:${input.code}`;
}

/** 读取排序字段用于重复检测。 */
function sortField(input: NonNullable<EquitySearchRequestDto['sort']>[number]): string {
  return input.field;
}

/** 校验事件日期、知识时间、事件族和最多十年的查询窗口。 */
function assertEventRequest(input: EquityEventSearchRequestDto): void {
  assertKnownAt(input.knownAt);
  assertUnique(input.families);
  if (input.start > input.end) invalidRequest();
  const maximumEnd = new Date(`${input.start}T00:00:00Z`);
  maximumEnd.setUTCFullYear(maximumEnd.getUTCFullYear() + 10);
  if (new Date(`${input.end}T00:00:00Z`) > maximumEnd) invalidRequest();
}

/** 拒绝未来知识时刻，避免向下游请求平台尚未知的 PIT 视图。 */
function assertKnownAt(knownAt: string | undefined): void {
  if (knownAt !== undefined && Date.parse(knownAt) > Date.now()) invalidRequest();
}

/** 限制条件请求头，避免无界值进入内部服务和日志。 */
function assertIfNoneMatch(ifNoneMatch: string | undefined): void {
  if (ifNoneMatch !== undefined && (ifNoneMatch.length < 1 || ifNoneMatch.length > 256)) {
    invalidRequest();
  }
}

/** 校验可选数组中没有重复项。 */
function assertUnique(values: readonly string[] | undefined): void {
  if (values !== undefined && new Set(values).size !== values.length) invalidRequest();
}

/** 统一抛出不泄漏内部校验结构的公开 400。 */
function invalidRequest(): never {
  throw new PublicProblemException(400, 'validation-error', 'Equity workspace request is invalid');
}
