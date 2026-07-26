import type { CookieOptions, Request, Response } from 'express';

import type { AppConfigService } from '../../platform/config/app-config.service.js';

export const REFRESH_COOKIE_NAME = 'refresh_token';

export function setRefreshCookie(
  response: Response,
  refreshToken: string,
  expiresAt: Date,
  config: AppConfigService,
): void {
  response.cookie(REFRESH_COOKIE_NAME, refreshToken, cookieOptions(config, expiresAt));
}

export function clearRefreshCookie(response: Response, config: AppConfigService): void {
  response.clearCookie(REFRESH_COOKIE_NAME, cookieOptions(config));
}

export function readRefreshCookie(request: Request): string | null {
  const header = request.header('cookie');
  if (!header) {
    return null;
  }
  for (const segment of header.split(';')) {
    const [name, ...value] = segment.trim().split('=');
    if (name === REFRESH_COOKIE_NAME && value.length > 0) {
      return decodeURIComponent(value.join('='));
    }
  }
  return null;
}

function cookieOptions(config: AppConfigService, expires?: Date): CookieOptions {
  return {
    httpOnly: true,
    secure: config.cookieSecure,
    sameSite: config.cookieSameSite,
    path: `/${config.apiPrefix}/auth`,
    ...(expires === undefined ? {} : { expires }),
  };
}
