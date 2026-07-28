import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { EquityInstrumentController } from '../equity-instrument.controller.js';
import type { EquityInstrumentService } from '../equity-instrument.service.js';

// 汇集证券公开 POST 条件读取响应的协议映射测试。
describe('EquityInstrumentController conditional POST response', () => {
  // 验证下游条件 GET 的 304 不会泄漏到公开 POST，而是映射为无体 204。
  it('maps downstream 304 to public 204', async () => {
    const equities = {
      listEquities: vi.fn().mockResolvedValue({ status: 304, etag: '"equities-v1"' }),
    };
    const controller = new EquityInstrumentController(
      equities as unknown as EquityInstrumentService,
    );
    const output = response();

    await expect(
      controller.list(
        {} as never,
        '"equities-v1"',
        { requestId: 'request-1' } as never,
        output.value,
      ),
    ).resolves.toBeUndefined();

    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
  });
});

/** 构造条件响应测试所需的最小 Express 响应与独立 spy。 */
function response(): {
  value: Response;
  status: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
} {
  const send = vi.fn();
  const status = vi.fn().mockReturnValue({ send });
  return {
    value: { setHeader: vi.fn(), status } as never,
    status,
    send,
  };
}
