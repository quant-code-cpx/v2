import { UnauthorizedException } from '@nestjs/common';
import type { Request, Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../../platform/config/app-config.service.js';
import { AuthController } from '../auth.controller.js';

const configuration = {
  apiPrefix: 'api/v1',
  cookieSameSite: 'lax',
  cookieSecure: false,
  jwtAccessTtlSeconds: 900,
} as AppConfigService;

// 汇集公开 Controller 边界的畸形 cookie 回归测试。
describe('AuthController refresh cookie handling', () => {
  // 验证非法百分号编码归一为匿名 refresh 输入，并保持 401 行为。
  it('passes malformed refresh cookies as null to refresh and returns 401', async () => {
    const auth = { refresh: vi.fn().mockRejectedValue(new UnauthorizedException()) };
    const controller = createController(auth);

    await expect(
      controller.refresh(requestWithCookie('refresh_token=%E0%A4%A'), response().response),
    ).rejects.toMatchObject({
      status: 401,
    });
    expect(auth.refresh).toHaveBeenCalledWith(null, '127.0.0.1', undefined);
  });

  // 验证畸形 cookie 不会破坏幂等退出，且浏览器 cookie 仍会清除。
  it('treats malformed refresh cookies as absent during logout and clears the cookie', async () => {
    const auth = { logout: vi.fn().mockResolvedValue(undefined) };
    const controller = createController(auth);
    const output = response();

    await expect(
      controller.logout(requestWithCookie('refresh_token=%E0%A4%A'), output.response),
    ).resolves.toBeUndefined();
    expect(auth.logout).toHaveBeenCalledWith(null, undefined);
    expect(output.clearCookieSpy).toHaveBeenCalledWith(
      'refresh_token',
      expect.objectContaining({ path: '/api/v1/auth' }),
    );
    expect(output.setHeaderSpy).toHaveBeenCalledWith('Cache-Control', 'no-store');
  });
});

/** 构造只包含 cookie 回归测试所需协作者方法的 AuthController。 */
function createController(auth: object): AuthController {
  return new AuthController(
    auth as never,
    {} as never,
    { assertAllowed: vi.fn() } as never,
    configuration,
  );
}

/** 构造同源请求 fixture，其原始 cookie header 可包含畸形百分号编码。 */
function requestWithCookie(cookie: string): Request {
  return {
    header: (name: string) => (name.toLowerCase() === 'cookie' ? cookie : undefined),
    ip: '127.0.0.1',
    socket: { remoteAddress: '127.0.0.1' },
  } as Request;
}

/** 构造响应与独立 spy，避免把 Express 实例方法当作未绑定测试回调。 */
function response(): {
  response: Response;
  clearCookieSpy: ReturnType<typeof vi.fn>;
  setHeaderSpy: ReturnType<typeof vi.fn>;
} {
  const clearCookieSpy = vi.fn();
  const setHeaderSpy = vi.fn();
  return {
    response: { clearCookie: clearCookieSpy, setHeader: setHeaderSpy } as never,
    clearCookieSpy,
    setHeaderSpy,
  };
}
