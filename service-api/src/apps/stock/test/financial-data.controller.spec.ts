import 'reflect-metadata';

import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { IS_PUBLIC_ROUTE } from '../../../common/decorators/public.decorator.js';
import { FinancialDataController } from '../financial-data.controller.js';
import type { FinancialDataService } from '../financial-data.service.js';

/** 覆盖四条财务公开 POST 路由的服务委派与 304 → 204 映射。 */
describe('FinancialDataController', () => {
  /** 验证所有读取保留请求标识并写出完整条件响应头。 */
  it('delegates financial reads and preserves version headers on 204', async () => {
    const financialData = {
      listReports: vi.fn().mockResolvedValue(notModified()),
      getReport: vi.fn().mockResolvedValue(notModified()),
      listMetrics: vi.fn().mockResolvedValue(notModified()),
      listValuations: vi.fn().mockResolvedValue(notModified()),
    };
    const controller = new FinancialDataController(
      financialData as unknown as FinancialDataService,
    );
    const equityPath = { exchange: 'SSE', symbol: '600519' } as never;
    const reportPath = {
      exchange: 'SSE',
      symbol: '600519',
      reportRef: '00000000-0000-4000-8000-000000000002',
    } as never;
    const request = { requestId: 'req-financial' } as never;
    const reportsResponse = response();

    await controller.listReports(equityPath, {} as never, '"old"', request, reportsResponse.value);
    await controller.getReport(reportPath, {} as never, undefined, request, response().value);
    await controller.listMetrics(equityPath, {} as never, undefined, request, response().value);
    await controller.listValuations(equityPath, {} as never, undefined, request, response().value);

    expect(financialData.listReports).toHaveBeenCalledWith(
      equityPath,
      {},
      '"old"',
      'req-financial',
    );
    expect(financialData.getReport).toHaveBeenCalledWith(
      reportPath,
      {},
      undefined,
      'req-financial',
    );
    expect(reportsResponse.setHeader).toHaveBeenCalledWith('ETag', '"financial"');
    expect(reportsResponse.setHeader).toHaveBeenCalledWith(
      'X-Data-Version',
      '00000000-0000-4000-8000-000000000001',
    );
    expect(reportsResponse.status).toHaveBeenCalledWith(204);
  });

  /** 验证四条财务路由没有进入匿名白名单，继续受全局 JWT 默认拒绝守卫保护。 */
  it('keeps every financial handler behind default-deny authentication', () => {
    const handlers = [
      handler(FinancialDataController.prototype, 'listReports'),
      handler(FinancialDataController.prototype, 'getReport'),
      handler(FinancialDataController.prototype, 'listMetrics'),
      handler(FinancialDataController.prototype, 'listValuations'),
    ];

    expect(Reflect.getMetadata(IS_PUBLIC_ROUTE, FinancialDataController)).toBeUndefined();
    for (const value of handlers) {
      expect(Reflect.getMetadata(IS_PUBLIC_ROUTE, value)).toBeUndefined();
    }
  });
});

/** 构造下游 304 条件读取结果。 */
function notModified(): {
  status: 304;
  etag: string;
  dataVersion: string;
} {
  return {
    status: 304,
    etag: '"financial"',
    dataVersion: '00000000-0000-4000-8000-000000000001',
  };
}

/** 构造条件响应映射所需的最小 Express 响应。 */
function response(): {
  value: Response;
  setHeader: ReturnType<typeof vi.fn>;
  status: ReturnType<typeof vi.fn>;
} {
  const send = vi.fn();
  const status = vi.fn().mockReturnValue({ send });
  const setHeader = vi.fn();
  return { value: { setHeader, status } as never, setHeader, status };
}

/** 通过 descriptor 读取方法，避免未绑定方法引用绕过静态检查。 */
function handler(target: object, name: string): object {
  const value: unknown = Object.getOwnPropertyDescriptor(target, name)?.value;
  if (typeof value !== 'function') throw new Error(`Missing method fixture: ${name}`);
  return value;
}
