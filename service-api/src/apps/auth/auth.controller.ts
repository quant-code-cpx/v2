import {
  Body,
  Controller,
  HttpCode,
  HttpStatus,
  Param,
  ParseUUIDPipe,
  Post,
  Req,
  Res,
} from '@nestjs/common';
import { ApiNoContentResponse, ApiTags } from '@nestjs/swagger';

import type { Request, Response } from 'express';

import { AppConfigService } from '../../config/app-config.service.js';
import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import { Public } from '../../common/decorators/public.decorator.js';
import {
  clearRefreshCookie,
  readRefreshCookie,
  setRefreshCookie,
} from '../../common/utils/refresh-cookie.js';
import { AuthService } from './auth.service.js';
import { BrowserRequestSecurityService } from './browser-request-security.service.js';
import { CaptchaService, type CaptchaChallenge } from './captcha.service.js';
import { LoginDto } from './dto/login.dto.js';
import { SessionListDto } from './dto/session-list.dto.js';
import type { RevokeOtherSessionsResult, SessionFamilyPage } from './auth.types.js';

type AccessTokenResponse = {
  accessToken: string;
  accessTokenExpiresIn: number;
  user: Awaited<ReturnType<AuthService['login']>>['user'];
};

@ApiTags('auth')
@Controller('auth')
export class AuthController {
  /** 将 HTTP handler 连接到鉴权编排与 refresh cookie 策略。 */
  public constructor(
    private readonly auth: AuthService,
    private readonly captcha: CaptchaService,
    private readonly browserSecurity: BrowserRequestSecurityService,
    private readonly config: AppConfigService,
  ) {}

  @Public()
  @Post('captcha')
  /** 校验浏览器来源安全策略后签发始终可见的 PNG CAPTCHA。 */
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
  /** 校验凭据、把 refresh token 写入 cookie，并在响应体返回 access token。 */
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
  /** 使用 cookie 中的 refresh token 轮换会话并写回后继 token。 */
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
  /** 幂等撤销 cookie 标识的会话；无论会话是否存在都清除 cookie。 */
  public async logout(
    @Req() request: Request,
    @Res({ passthrough: true }) response: Response,
  ): Promise<void> {
    this.browserSecurity.assertAllowed(browserRequest(request));
    await this.auth.logout(readRefreshCookie(request), request.requestId);
    clearRefreshCookie(response, this.config);
    response.setHeader('Cache-Control', 'no-store');
  }

  @Post('sessions/list')
  @HttpCode(HttpStatus.OK)
  /** 返回当前用户活动会话族，且不暴露设备、网络或认证秘密。 */
  public async listMySessionFamilies(
    @Req() request: AuthenticatedRequest,
    @Body() input: SessionListDto,
    @Res({ passthrough: true }) response: Response,
  ): Promise<SessionFamilyPage> {
    const result = await this.auth.listMySessionFamilies(request.user, input);
    response.setHeader('Cache-Control', 'no-store');
    return result;
  }

  @Post('sessions/:familyId/revoke')
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** 撤销本人指定会话族；目标为当前族时同步清除 refresh cookie。 */
  public async revokeMySessionFamily(
    @Req() request: AuthenticatedRequest,
    @Param('familyId', new ParseUUIDPipe({ version: '4' })) familyId: string,
    @Res({ passthrough: true }) response: Response,
  ): Promise<void> {
    const current = await this.auth.revokeMySessionFamily(
      request.user,
      familyId,
      request.requestId,
    );
    if (current) {
      clearRefreshCookie(response, this.config);
    }
    response.setHeader('Cache-Control', 'no-store');
  }

  @Post('sessions/revoke-others')
  @HttpCode(HttpStatus.OK)
  /** 保留当前会话族，幂等撤销本人其余活动会话族。 */
  public async revokeMyOtherSessionFamilies(
    @Req() request: AuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<RevokeOtherSessionsResult> {
    const result = await this.auth.revokeMyOtherSessionFamilies(request.user, request.requestId);
    response.setHeader('Cache-Control', 'no-store');
    return result;
  }
}

/** 优先选择 Express 解析的 IP，回退 socket 地址作为限流身份。 */
function clientIp(request: Request): string {
  return request.ip ?? request.socket.remoteAddress ?? 'unknown';
}

/** 只读取浏览器来源校验需要的规范化 header。 */
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

/** 将 CAPTCHA 绑定到网络与 user-agent 上下文，但不持久化任一原值。 */
function captchaContext(request: Request): { ip: string; userAgent: string } {
  return { ip: clientIp(request), userAgent: request.header('user-agent') ?? '' };
}
