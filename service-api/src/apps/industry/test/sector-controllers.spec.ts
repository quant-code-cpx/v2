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
      listSectors: vi.fn().mockResolvedValue({
        status: 304,
        etag: '"sectors-v1"',
        dataVersion: '00000000-0000-4000-8000-000000000001',
      }),
    };
    const controller = new SectorMarketDataController(
      sectors as unknown as SectorMarketDataService,
    );
    const output = response();

    await expect(
      controller.list(
        {} as never,
        '"sectors-v1"',
        { requestId: 'sector-controller-test' } as never,
        output.value,
      ),
    ).resolves.toBeUndefined();

    expect(sectors.listSectors).toHaveBeenCalledWith({}, '"sectors-v1"', 'sector-controller-test');
    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
    expect(output.setHeader).toHaveBeenCalledWith(
      'X-Data-Version',
      '00000000-0000-4000-8000-000000000001',
    );
  });

  // 验证证券板块归属把下游 304 映射为公开无体 204。
  it('maps equity membership downstream 304 to public 204', async () => {
    const sectors = {
      listEquitySectors: vi.fn().mockResolvedValue({
        status: 304,
        etag: '"membership-v1"',
        dataVersion: '00000000-0000-4000-8000-000000000002',
      }),
    };
    const controller = new EquitySectorMembershipController(
      sectors as unknown as SectorMarketDataService,
    );
    const output = response();

    await expect(
      controller.list(
        {} as never,
        {} as never,
        '"membership-v1"',
        { requestId: 'membership-controller-test' } as never,
        output.value,
      ),
    ).resolves.toBeUndefined();

    expect(sectors.listEquitySectors).toHaveBeenCalledWith(
      {},
      {},
      '"membership-v1"',
      'membership-controller-test',
    );
    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
    expect(output.setHeader).toHaveBeenCalledWith(
      'X-Data-Version',
      '00000000-0000-4000-8000-000000000002',
    );
  });
});

/** 构造条件响应测试所需的最小 Express 响应与独立 spy。 */
function response(): {
  value: Response;
  status: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  setHeader: ReturnType<typeof vi.fn>;
} {
  const send = vi.fn();
  const status = vi.fn().mockReturnValue({ send });
  const setHeader = vi.fn();
  return {
    value: { setHeader, status } as never,
    status,
    send,
    setHeader,
  };
}
