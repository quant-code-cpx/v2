import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { SwIndustryController } from '../sw-industry.controller.js';
import type { SwIndustryService } from '../sw-industry.service.js';

/** 覆盖公开申万 POST 的条件响应和版本响应头映射。 */
describe('SwIndustryController', () => {
  /** 验证内部 GET 304 被映射为公开 POST 204。 */
  it('maps downstream 304 to public 204', async () => {
    const service = {
      listIndustries: vi.fn().mockResolvedValue({ status: 304, etag: '"sw-v1"' }),
    };
    const controller = new SwIndustryController(service as unknown as SwIndustryService);
    const output = response();

    await expect(
      controller.list(
        { limit: 100 },
        '"sw-v1"',
        { requestId: 'sw-controller-test' } as never,
        output.value,
      ),
    ).resolves.toBeUndefined();

    expect(output.status).toHaveBeenCalledWith(204);
    expect(output.send).toHaveBeenCalledOnce();
  });

  /** 验证成功页复制 ETag 与 release dataVersion，不暴露内部凭据。 */
  it('writes data version for a successful taxonomy page', async () => {
    const service = {
      listIndustries: vi.fn().mockResolvedValue({
        status: 200,
        etag: '"sw-v2"',
        body: {
          release: { dataVersion: '00000000-0000-4000-8000-000000000001' },
        },
      }),
    };
    const controller = new SwIndustryController(service as unknown as SwIndustryService);
    const output = response();

    await controller.list(
      { limit: 100 },
      undefined,
      { requestId: 'sw-controller-test' } as never,
      output.value,
    );

    expect(output.setHeader).toHaveBeenCalledWith(
      'X-Data-Version',
      '00000000-0000-4000-8000-000000000001',
    );
  });
});

/** 构造申万控制器测试所需的最小 Express Response 与 spy。 */
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
