import { RequestMethod } from '@nestjs/common';
import { METHOD_METADATA, PATH_METADATA } from '@nestjs/common/constants.js';
import type { Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import { EquityWorkspaceController } from '../equity-workspace.controller.js';
import type { EquityWorkspaceService } from '../equity-workspace.service.js';

/** 覆盖股票中心三条公开 POST 的委派与缓存头语义。 */
describe('EquityWorkspaceController', () => {
  /** 三条股票中心公开路由必须位于 equities 下且只声明 POST。 */
  it('declares the frozen public routes as POST only', () => {
    const routes = [
      ['search', 'search'],
      ['searchEvents', ':exchange/:symbol/events/search'],
      ['getDataStatus', ':exchange/:symbol/data-status'],
    ] as const;

    expect(Reflect.getMetadata(PATH_METADATA, EquityWorkspaceController) as unknown).toBe(
      'equities',
    );
    for (const [name, path] of routes) {
      const handler = Object.getOwnPropertyDescriptor(EquityWorkspaceController.prototype, name)
        ?.value as unknown;
      if (typeof handler !== 'function') throw new TypeError('Expected route handler');
      expect(Reflect.getMetadata(PATH_METADATA, handler) as unknown).toBe(path);
      expect(Reflect.getMetadata(METHOD_METADATA, handler) as unknown).toBe(RequestMethod.POST);
    }
  });

  /** search 应保留认证用户和 requestId，并对无 publication 使用三十秒私有复验。 */
  it('delegates search and writes unavailable cache metadata', async () => {
    const workspace = {
      search: vi.fn().mockResolvedValue({
        status: 200,
        etag: undefined,
        dataVersion: undefined,
        body: {
          availability: 'UNAVAILABLE',
          reasonCode: 'NO_PUBLICATION',
          release: null,
          records: [],
        },
      }),
    };
    const controller = new EquityWorkspaceController(
      workspace as unknown as EquityWorkspaceService,
    );
    const response = responseFixture();
    const request = {
      requestId: 'req-search',
      user: { userId: 'user-1' },
    } as never;

    const body = await controller.search({}, undefined, request, response.value);

    expect(workspace.search).toHaveBeenCalledWith({}, undefined, 'req-search', 'user-1');
    expect(body).toMatchObject({ availability: 'UNAVAILABLE' });
    expect(response.setHeader).toHaveBeenCalledWith(
      'Cache-Control',
      'private, max-age=30, must-revalidate',
    );
  });

  /** 下游 304 应映射为公开 POST 204，并保留 ETag 与 X-Data-Version。 */
  it('maps conditional data-status reads to 204', async () => {
    const workspace = {
      getDataStatus: vi.fn().mockResolvedValue({
        status: 304,
        etag: '"status-v1"',
        dataVersion: '00000000-0000-4000-8000-000000000001',
      }),
    };
    const controller = new EquityWorkspaceController(
      workspace as unknown as EquityWorkspaceService,
    );
    const response = responseFixture();
    const request = {
      requestId: 'req-status',
      user: { userId: 'user-1' },
    } as never;

    const body = await controller.getDataStatus(
      { exchange: 'SSE', symbol: '600519' },
      {},
      '"status-v1"',
      request,
      response.value,
    );

    expect(body).toBeUndefined();
    expect(workspace.getDataStatus).toHaveBeenCalledWith(
      { exchange: 'SSE', symbol: '600519' },
      {},
      '"status-v1"',
      'req-status',
      'user-1',
    );
    expect(response.setHeader).toHaveBeenCalledWith('ETag', '"status-v1"');
    expect(response.setHeader).toHaveBeenCalledWith(
      'X-Data-Version',
      '00000000-0000-4000-8000-000000000001',
    );
    expect(response.status).toHaveBeenCalledWith(204);
    expect(response.send).toHaveBeenCalledOnce();
  });
});

/** 构造条件响应写入所需的最小 Express Response。 */
function responseFixture(): {
  value: Response;
  setHeader: ReturnType<typeof vi.fn>;
  status: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
} {
  const setHeader = vi.fn();
  const send = vi.fn();
  const status = vi.fn();
  status.mockReturnValue({ send });
  return {
    value: { setHeader, status } as never,
    setHeader,
    status,
    send,
  };
}
