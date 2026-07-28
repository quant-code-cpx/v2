import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { MoneyFlowController } from '../money-flow.controller.js';
import type { MoneyFlowService } from '../money-flow.service.js';

const dataVersion = '00000000-0000-4000-8000-000000000017';

/** 覆盖五条资金流公开 POST 的服务委派与条件响应映射。 */
describe('MoneyFlowController', () => {
  /** 验证五条读取都保留请求标识，且内部 304 映射为公开 204。 */
  it('delegates every read and maps internal 304 to public 204', async () => {
    const moneyFlow = {
      listMethodologies: vi.fn().mockResolvedValue(notModified()),
      listEquityDaily: vi.fn().mockResolvedValue(notModified()),
      listSectorDaily: vi.fn().mockResolvedValue(notModified()),
      listMarketDaily: vi.fn().mockResolvedValue(notModified()),
      listRanking: vi.fn().mockResolvedValue(notModified()),
    };
    const controller = new MoneyFlowController(moneyFlow as unknown as MoneyFlowService);
    const request = { requestId: 'req-money-flow' } as never;
    const output = response();

    await controller.listMethodologies({} as never, '"old"', request, output.value);
    await controller.listEquityDaily(
      {} as never,
      {} as never,
      undefined,
      request,
      response().value,
    );
    await controller.listSectorDaily(
      {} as never,
      {} as never,
      undefined,
      request,
      response().value,
    );
    await controller.listMarketDaily(
      {} as never,
      {} as never,
      undefined,
      request,
      response().value,
    );
    await controller.listRanking({} as never, {} as never, undefined, request, response().value);

    expect(moneyFlow.listMethodologies).toHaveBeenCalledWith({}, '"old"', 'req-money-flow');
    expect(moneyFlow.listEquityDaily).toHaveBeenCalledWith({}, {}, undefined, 'req-money-flow');
    expect(moneyFlow.listSectorDaily).toHaveBeenCalledWith({}, {}, undefined, 'req-money-flow');
    expect(moneyFlow.listMarketDaily).toHaveBeenCalledWith({}, {}, undefined, 'req-money-flow');
    expect(moneyFlow.listRanking).toHaveBeenCalledWith({}, {}, undefined, 'req-money-flow');
    expect(output.setHeader).toHaveBeenCalledWith('ETag', '"money-flow-v1"');
    expect(output.setHeader).toHaveBeenCalledWith('X-Data-Version', dataVersion);
    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
  });
});

/** 构造内部条件命中结果。 */
function notModified(): {
  status: 304;
  etag: string;
  dataVersion: string;
} {
  return { status: 304, etag: '"money-flow-v1"', dataVersion };
}

/** 构造条件响应映射所需的最小 Express 响应。 */
function response(): {
  value: Response;
  setHeader: ReturnType<typeof vi.fn>;
  status: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
} {
  const send = vi.fn();
  const status = vi.fn().mockReturnValue({ send });
  const setHeader = vi.fn();
  return {
    value: { setHeader, status } as never,
    setHeader,
    status,
    send,
  };
}
