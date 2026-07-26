import type { CookieOptions, Request, Response } from 'express';

import type { AppConfigService } from '../config/app-config.service.js';

export const REFRESH_COOKIE_NAME = 'refresh_token';

/** Store refresh token with common security options and its absolute expiry. */
export function setRefreshCookie(
  response: Response,
  refreshToken: string,
  expiresAt: Date,
  config: AppConfigService,
): void {
  response.cookie(REFRESH_COOKIE_NAME, refreshToken, cookieOptions(config, expiresAt));
}

/** Delete refresh cookie using identical scope attributes. */
export function clearRefreshCookie(response: Response, config: AppConfigService): void {
  response.clearCookie(REFRESH_COOKIE_NAME, cookieOptions(config));
}

/** Extract refresh token from raw Cookie header without adding parser middleware. */
export function readRefreshCookie(request: Request): string | null {
  const header = request.header('cookie');
  if (!header) {
    return null;
  }
  for (const segment of header.split(';')) {
    const [name, ...value] = segment.trim().split('=');
    if (name === REFRESH_COOKIE_NAME && value.length > 0) {
      try {
        return decodeURIComponent(value.join('='));
      } catch {
        // Treat malformed client cookie encoding as an absent token so auth endpoints keep their stable failures.
        return null;
      }
    }
  }
  return null;
}

/** Build shared cookie scope required for setting and reliably clearing refresh tokens. */
function cookieOptions(config: AppConfigService, expires?: Date): CookieOptions {
  return {
    httpOnly: true,
    secure: config.cookieSecure,
    sameSite: config.cookieSameSite,
    path: `/${config.apiPrefix}/auth`,
    ...(expires === undefined ? {} : { expires }),
  };
}
