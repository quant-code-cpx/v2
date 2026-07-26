import { UnauthorizedException } from '@nestjs/common';
import type { Request, Response } from 'express';
import { describe, expect, it, vi } from 'vitest';

import type { AppConfigService } from '../../platform/config/app-config.service.js';
import { AuthController } from './auth.controller.js';

const configuration = {
  apiPrefix: 'api/v1',
  cookieSameSite: 'lax',
  cookieSecure: false,
  jwtAccessTtlSeconds: 900,
} as AppConfigService;

// Group malformed-cookie regressions at the public controller boundary.
describe('AuthController refresh cookie handling', () => {
  // Verify invalid percent encoding becomes the same anonymous refresh input and preserves 401 behavior.
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

  // Verify malformed cookies cannot make idempotent logout fail and the browser cookie is still cleared.
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

/** Build an AuthController with only the collaborator methods exercised by each cookie regression. */
function createController(auth: object): AuthController {
  return new AuthController(
    auth as never,
    {} as never,
    { assertAllowed: vi.fn() } as never,
    configuration,
  );
}

/** Build a same-origin-like request fixture whose raw cookie header can contain malformed percent encoding. */
function requestWithCookie(cookie: string): Request {
  return {
    header: (name: string) => (name.toLowerCase() === 'cookie' ? cookie : undefined),
    ip: '127.0.0.1',
    socket: { remoteAddress: '127.0.0.1' },
  } as Request;
}

/** Build a response plus detached spies to avoid treating Express instance methods as unbound test callbacks. */
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
