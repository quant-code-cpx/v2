import { BadRequestException, Injectable } from '@nestjs/common';

import {
  FinancialDataClient,
  type FinancialConditionalRead,
} from '../../data-sync/clients/financial-data.client.js';
import type {
  FinancialMetricPage,
  FinancialReportDetail,
  FinancialReportPage,
  ValuationPage,
} from '../../data-sync/contracts/financial-data.contract.js';
import type { EquityPathDto } from './dto/equity-path.dto.js';
import type { FinancialReportPathDto } from './dto/financial-report-path.dto.js';
import type { GetFinancialReportQueryDto } from './dto/get-financial-report-query.dto.js';
import type { ListFinancialMetricsQueryDto } from './dto/list-financial-metrics-query.dto.js';
import type { ListFinancialReportsQueryDto } from './dto/list-financial-reports-query.dto.js';
import type { ListValuationsQueryDto } from './dto/list-valuations-query.dto.js';

/** 编排认证用户对报表、指标和估值的点时读取。 */
@Injectable()
export class FinancialDataService {
  /** 注入唯一允许访问同步服务财务契约的防腐 client。 */
  public constructor(private readonly client: FinancialDataClient) {}

  /** 读取一个方法学下的已发布报表页。 */
  public listReports(
    path: EquityPathDto,
    query: ListFinancialReportsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<FinancialConditionalRead<FinancialReportPage>> {
    assertDateRange(query.reportPeriodFrom, query.reportPeriodTo, 'reportPeriod');
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listReports({
      exchange: path.exchange,
      symbol: path.symbol,
      statementTypes: query.statementType,
      periodBases: query.basis,
      scope: query.scope,
      methodologyCode: query.methodologyCode,
      methodologyVersion: query.methodologyVersion,
      reportPeriodFrom: query.reportPeriodFrom,
      reportPeriodTo: query.reportPeriodTo,
      asOf: query.asOf,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取一份已发布报表的治理字段页。 */
  public getReport(
    path: FinancialReportPathDto,
    query: GetFinancialReportQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<FinancialConditionalRead<FinancialReportDetail>> {
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.getReport({
      exchange: path.exchange,
      symbol: path.symbol,
      reportRef: path.reportRef,
      metrics: query.metric,
      asOf: query.asOf,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取显式供应商或平台方法学下的指标页。 */
  public listMetrics(
    path: EquityPathDto,
    query: ListFinancialMetricsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<FinancialConditionalRead<FinancialMetricPage>> {
    assertDateRange(query.reportPeriodFrom, query.reportPeriodTo, 'reportPeriod');
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listMetrics({
      exchange: path.exchange,
      symbol: path.symbol,
      origin: query.origin,
      methodologyCode: query.methodologyCode,
      methodologyVersion: query.methodologyVersion,
      metrics: query.metric,
      periodBases: query.basis,
      reportPeriodFrom: query.reportPeriodFrom,
      reportPeriodTo: query.reportPeriodTo,
      asOf: query.asOf,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }

  /** 读取显式估值方法学下的包含端日期窗口。 */
  public listValuations(
    path: EquityPathDto,
    query: ListValuationsQueryDto,
    ifNoneMatch: string | undefined,
    requestId: string,
  ): Promise<FinancialConditionalRead<ValuationPage>> {
    assertDateRange(query.start, query.end, 'valuation');
    if (daysBetween(query.start, query.end) > 3660) {
      throw new BadRequestException('valuation date span exceeds 3660 days');
    }
    assertKnownAt(query.knownAt);
    assertIfNoneMatch(ifNoneMatch);
    return this.client.listValuations({
      exchange: path.exchange,
      symbol: path.symbol,
      methodologyCode: query.methodologyCode,
      methodologyVersion: query.methodologyVersion,
      metrics: query.metric,
      start: query.start,
      end: query.end,
      asOf: query.asOf,
      knownAt: query.knownAt,
      cursor: query.cursor,
      limit: query.limit,
      ifNoneMatch,
      requestId,
    });
  }
}

/** 拒绝倒置的可选或必填日期范围。 */
function assertDateRange(start: string | undefined, end: string | undefined, name: string): void {
  if (start !== undefined && end !== undefined && start > end) {
    throw new BadRequestException(`${name} start must not be after end`);
  }
}

/** 计算两个严格 ISO 日期之间的 UTC 日数，仅用于限制公开负载。 */
function daysBetween(start: string, end: string): number {
  return (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000;
}

/** 拒绝未来知识时刻，防止点时查询形成前视读取。 */
function assertKnownAt(knownAt: string | undefined): void {
  if (knownAt !== undefined && Date.parse(knownAt) > Date.now()) {
    throw new BadRequestException('knownAt must not be in the future');
  }
}

/** 限制条件请求头长度，避免无界透传。 */
function assertIfNoneMatch(ifNoneMatch: string | undefined): void {
  if (ifNoneMatch !== undefined && (ifNoneMatch.length < 1 || ifNoneMatch.length > 256)) {
    throw new BadRequestException('If-None-Match is invalid');
  }
}
