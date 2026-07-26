import { Body, Controller, HttpCode, HttpStatus, Post, Req, Res } from '@nestjs/common';
import { ApiNoContentResponse, ApiTags } from '@nestjs/swagger';

import type { Request, Response } from 'express';

import { AppConfigService } from '../../platform/config/app-config.service.js';
import { Public } from '../../platform/http/public.decorator.js';
import {
  clearRefreshCookie,
  readRefreshCookie,
  setRefreshCookie,
} from '../../platform/http/refresh-cookie.js';
import { AuthService } from './auth.service.js';
import { BrowserRequestSecurityService } from './browser-request-security.service.js';
import { CaptchaService, type CaptchaChallenge } from './captcha.service.js';
import { LoginDto } from './dto/login.dto.js';

type AccessTokenResponse = {
  accessToken: string;
  accessTokenExpiresIn: number;
  user: Awaited<ReturnType<AuthService['login']>>['user'];
};

@ApiTags('auth')
@Controller('auth')
export class AuthController {
  /** Wire HTTP handlers to authentication orchestration and cookie settings. */
  public constructor(
    private readonly auth: AuthService,
    private readonly captcha: CaptchaService,
    private readonly browserSecurity: BrowserRequestSecurityService,
    private readonly config: AppConfigService,
  ) {}

  @Public()
  @Post('captcha')
  /** Issue the always-visible PNG CAPTCHA after enforcing browser-origin security controls. */
  public async createCaptcha(
    @Req() request: Request,
    @Res({ passthrough: true }) response: Response,
  ): Promise<CaptchaChallenge> {
    this.browserSecurity.assertAllowed(browserRequest(request));
    const challenge = await this.captcha.createChallenge(captchaContext(request));
    response.setHeader('Cache-Control', 'no-store');
    return challenge;
  }

  @Public()
  @Post('login')
  @HttpCode(HttpStatus.OK)
  /** Authenticate credentials, persist refresh token in cookie, return access token body. */
  public async login(
    @Body() input: LoginDto,
    @Req() request: Request,
    @Res({ passthrough: true }) response: Response,
  ): Promise<AccessTokenResponse> {
    this.browserSecurity.assertAllowed(browserRequest(request), true);
    const tokens = await this.auth.login(
      input.account,
      input.password,
      input.captchaId,
      input.captchaAnswer,
      clientIp(request),
      captchaContext(request),
      request.requestId,
    );
    setRefreshCookie(response, tokens.refreshToken, tokens.refreshExpiresAt, this.config);
    response.setHeader('Cache-Control', 'no-store');
    return {
      accessToken: tokens.accessToken,
      accessTokenExpiresIn: this.config.jwtAccessTtlSeconds,
      user: tokens.user,
    };
  }

  @Public()
  @Post('refresh')
  @HttpCode(HttpStatus.OK)
  /** Rotate refresh session from cookie and replace cookie with successor token. */
  public async refresh(
    @Req() request: Request,
    @Res({ passthrough: true }) response: Response,
  ): Promise<AccessTokenResponse> {
    this.browserSecurity.assertAllowed(browserRequest(request));
    const refreshToken = readRefreshCookie(request);
    const tokens = await this.auth.refresh(refreshToken, clientIp(request), request.requestId);
    setRefreshCookie(response, tokens.refreshToken, tokens.refreshExpiresAt, this.config);
    response.setHeader('Cache-Control', 'no-store');
    return {
      accessToken: tokens.accessToken,
      accessTokenExpiresIn: this.config.jwtAccessTtlSeconds,
      user: tokens.user,
    };
  }

  @Public()
  @Post('logout')
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** Idempotently revoke a cookie-identified session when present and always clear the cookie. */
  public async logout(
    @Req() request: Request,
    @Res({ passthrough: true }) response: Response,
  ): Promise<void> {
    this.browserSecurity.assertAllowed(browserRequest(request));
    await this.auth.logout(readRefreshCookie(request), request.requestId);
    clearRefreshCookie(response, this.config);
    response.setHeader('Cache-Control', 'no-store');
  }
}

/** Select Express-resolved IP, then socket address, for rate-limit identity. */
function clientIp(request: Request): string {
  return request.ip ?? request.socket.remoteAddress ?? 'unknown';
}

/** Read only normalized headers relevant to browser-origin enforcement. */
function browserRequest(request: Request): {
  contentType: string | undefined;
  fetchSite: string | undefined;
  origin: string | undefined;
} {
  return {
    contentType: request.header('content-type'),
    fetchSite: request.header('sec-fetch-site'),
    origin: request.header('origin'),
  };
}

/** Bind each CAPTCHA to network and user-agent context without persisting either field. */
function captchaContext(request: Request): { ip: string; userAgent: string } {
  return { ip: clientIp(request), userAgent: request.header('user-agent') ?? '' };
}
