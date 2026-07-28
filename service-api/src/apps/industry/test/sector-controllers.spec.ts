import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { EquitySectorMembershipController } from '../equity-sector-membership.controller.js';
import { SectorMarketDataController } from '../sector-market-data.controller.js';
import type { SectorMarketDataService } from '../sector-market-data.service.js';

// 汇集板块公开 POST 条件读取响应的协议映射测试。
describe('industry controllers conditional POST response', () => {
  // 验证板块目录把下游 304 映射为公开无体 204。
  it('maps sector catalog downstream 304 to public 204', async () => {
    const sectors = {
      listSectors: vi.fn().mockResolvedValue({ status: 304, etag: '"sectors-v1"' }),
    };
    const controller = new SectorMarketDataController(
      sectors as unknown as SectorMarketDataService,
    );
    const output = response();

    await expect(
      controller.list({} as never, '"sectors-v1"', output.value),
    ).resolves.toBeUndefined();

    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
  });

  // 验证证券板块归属把下游 304 映射为公开无体 204。
  it('maps equity membership downstream 304 to public 204', async () => {
    const sectors = {
      listEquitySectors: vi.fn().mockResolvedValue({ status: 304, etag: '"membership-v1"' }),
    };
    const controller = new EquitySectorMembershipController(
      sectors as unknown as SectorMarketDataService,
    );
    const output = response();

    await expect(
      controller.list({} as never, {} as never, '"membership-v1"', output.value),
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
