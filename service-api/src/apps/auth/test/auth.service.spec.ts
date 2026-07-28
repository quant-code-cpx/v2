/* eslint-disable @typescript-eslint/no-unsafe-assignment -- Vitest asymmetric matchers intentionally return `any`. */

import { Role, UserStatus } from '../../../generated/prisma/client.js';
import type { AppConfigService } from '../../../config/app-config.service.js';
import type { DatabaseService } from '../../../shared/database/database.service.js';
import type { RedisService } from '../../../shared/redis/redis.service.js';
import type { UserService } from '../../user/user.service.js';
import type { CurrentUserResource } from '../../user/user.types.js';
import { createHash } from 'node:crypto';
import { describe, expect, it, vi } from 'vitest';

import { AuthService } from '../auth.service.js';
import { CaptchaService, type CaptchaCodeGenerator } from '../captcha.service.js';
import type { SecurityRateLimitService } from '../security-rate-limit.service.js';

const configuration = {
  captchaHmacSecret: 'test-only-captcha-hmac-secret-long-enough-for-hmac-use',
  captchaTtlSeconds: 120,
  jwtAccessTtlSeconds: 900,
  refreshTokenTtlSeconds: 3_600,
  refreshRaceGraceSeconds: 5,
} as AppConfigService;

const context = { ip: '127.0.0.1', userAgent: 'fixture-browser' };

// 汇集测试专用验证码注入下的完整登录成功与验证码优先失败行为。
describe('AuthService login', () => {
  // 验证已知 fixture 答案依次通过验证码、规范化账号鉴权、会话与安全响应数据流程。
  it('logs in with a test-injected CAPTCHA fixture without exposing its answer in output', async () => {
    const fixture = createFixture();
    const challenge = await fixture.captcha.createChallenge(context);

    const result = await fixture.auth.login(
      '  APEX.ADMIN ',
      'safe-password-2026',
      challenge.challengeId,
      '2468',
      context.ip,
      context,
      'request-1',
    );

    expect(fixture.users.authenticate).toHaveBeenCalledWith('apex.admin', 'safe-password-2026');
    expect(fixture.sessionCreate).toHaveBeenCalledOnce();
    expect(result).toMatchObject({
      accessToken: 'signed-access-token',
      user: { account: 'apex.admin', role: Role.SUPER_ADMIN },
    });
    expect(result).not.toHaveProperty('captchaAnswer');
  });

  // 验证验证码失败发生在账号查询前，不能形成鉴权绕过。
  it('rejects wrong CAPTCHA before credential lookup and consumes its challenge', async () => {
    const fixture = createFixture();
    const challenge = await fixture.captcha.createChallenge(context);

    await expect(
      fixture.auth.login(
        'apex.admin',
        'safe-password-2026',
        challenge.challengeId,
        '1111',
        context.ip,
        context,
      ),
    ).rejects.toMatchObject({ status: 422 });
    expect(fixture.users.authenticate).not.toHaveBeenCalled();
    await expect(
      fixture.captcha.verifyAndConsume(challenge.challengeId, '2468', context),
    ).resolves.toBe(false);
  });

  // 验证退出把首次合法请求与重复请求都视为幂等 cookie 清理。
  it('revokes valid logout session once while allowing repeat logout', async () => {
    const secret = 'opaque-refresh-secret';
    const session = {
      id: '00000000-0000-4000-8000-000000000012',
      userId: '00000000-0000-4000-8000-000000000001',
      refreshTokenHash: digest(secret),
    };
    const updateMany = vi.fn().mockResolvedValue({ count: 1 });
    const auditCreate = vi.fn().mockResolvedValue({});
    const transaction = { session: { updateMany }, auditLog: { create: auditCreate } };
    const auth = minimalAuth({
      session: { findUnique: vi.fn().mockResolvedValue(session) },
      $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
        callback(transaction),
    });

    await expect(auth.logout(`${session.id}.${secret}`, 'request-1')).resolves.toBeUndefined();
    await expect(auth.logout(null, 'request-2')).resolves.toBeUndefined();
    expect(updateMany).toHaveBeenCalledOnce();
    expect(auditCreate).toHaveBeenCalledWith(
      expect.objectContaining({ data: expect.objectContaining({ action: 'auth.logout' }) }),
    );
  });

  // 验证并发 refresh 更新返回可重试的 409，而不是静默创建两个后继会话。
  it('returns refresh-race when compare-and-swap loses within grace period', async () => {
    const secret = 'opaque-refresh-secret';
    const session = {
      id: '00000000-0000-4000-8000-000000000013',
      userId: '00000000-0000-4000-8000-000000000001',
      refreshTokenHash: digest(secret),
      familyId: '00000000-0000-4000-8000-000000000013',
      securityVersion: 1,
      expiresAt: new Date(Date.now() + 60_000),
      absoluteExpiresAt: new Date(Date.now() + 60_000),
      revokedAt: null,
      rotatedAt: null,
    };
    const latest = { ...session, revokedAt: new Date(), rotatedAt: new Date() };
    const transaction = {
      session: { updateMany: vi.fn().mockResolvedValue({ count: 0 }), create: vi.fn() },
      auditLog: { create: vi.fn() },
    };
    const users = {
      getAuthenticationSnapshot: vi.fn().mockResolvedValue({
        id: session.userId,
        account: 'apex.admin',
        displayName: 'Super Administrator',
        role: Role.SUPER_ADMIN,
        status: UserStatus.ACTIVE,
        securityVersion: 1,
      }),
    };
    const rateLimit = {
      assertRefreshAllowed: vi.fn().mockResolvedValue(undefined),
      isRefreshReplayMarked: vi.fn().mockResolvedValue(false),
      markRefreshReplay: vi.fn().mockResolvedValue(undefined),
    } as unknown as SecurityRateLimitService;
    const auth = new AuthService(
      {
        client: {
          session: {
            findUnique: vi.fn().mockResolvedValueOnce(session).mockResolvedValueOnce(latest),
          },
          $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
            callback(transaction),
        },
      } as unknown as DatabaseService,
      users as unknown as UserService,
      {} as CaptchaService,
      {} as never,
      configuration,
      rateLimit,
    );

    await expect(auth.refresh(`${session.id}.${secret}`, context.ip)).rejects.toMatchObject({
      status: 409,
    });
    expect(transaction.session.create).not.toHaveBeenCalled();
  });
});

/** 构造隔离的 AuthService 协作者，并将验证码 provider fixture 限制在本测试模块。 */
function createFixture(): {
  auth: AuthService;
  captcha: CaptchaService;
  sessionCreate: ReturnType<typeof vi.fn>;
  users: { authenticate: ReturnType<typeof vi.fn> };
} {
  const redis = new FakeRedis();
  const captchaRateLimit = {
    assertCaptchaIssueAllowed: (): Promise<void> => Promise.resolve(),
  } as unknown as SecurityRateLimitService;
  const captcha = new CaptchaService(
    redis as unknown as RedisService,
    captchaRateLimit,
    configuration,
    new FixtureCaptchaCodeGenerator(),
  );
  const sessionCreate = vi.fn().mockResolvedValue({});
  const transaction = {
    session: { create: sessionCreate },
    user: { update: vi.fn().mockResolvedValue({}) },
    auditLog: { create: vi.fn().mockResolvedValue({}) },
  };
  const database = {
    client: {
      $transaction: (callback: (value: typeof transaction) => Promise<unknown>) =>
        callback(transaction),
    },
  } as unknown as DatabaseService;
  const currentUser: CurrentUserResource = {
    id: '00000000-0000-4000-8000-000000000001',
    account: 'apex.admin',
    displayName: 'Super Administrator',
    role: Role.SUPER_ADMIN,
    status: UserStatus.ACTIVE,
    version: 1,
    lastLoginAt: null,
    deletedAt: null,
    createdAt: '2026-07-26T00:00:00.000Z',
    updatedAt: '2026-07-26T00:00:00.000Z',
    permissions: ['profile:read'],
  };
  const users = {
    authenticate: vi.fn().mockResolvedValue({
      id: currentUser.id,
      account: currentUser.account,
      displayName: currentUser.displayName,
      role: currentUser.role,
      status: currentUser.status,
      securityVersion: 1,
    }),
    getMe: vi.fn().mockResolvedValue(currentUser),
  };
  const rateLimit = {
    assertLoginAllowed: vi.fn().mockResolvedValue(undefined),
    resetLoginFailures: vi.fn().mockResolvedValue(undefined),
    recordFailedLogin: vi.fn().mockResolvedValue(undefined),
    assertRefreshAllowed: vi.fn().mockResolvedValue(undefined),
    isRefreshReplayMarked: vi.fn().mockResolvedValue(false),
    markRefreshReplay: vi.fn().mockResolvedValue(undefined),
  } as unknown as SecurityRateLimitService;
  const jwt = { signAsync: vi.fn().mockResolvedValue('signed-access-token') };
  return {
    auth: new AuthService(
      database,
      users as unknown as UserService,
      captcha,
      jwt as never,
      configuration,
      rateLimit,
    ),
    captcha,
    sessionCreate,
    users,
  };
}

/** 以等同生产 Redis 脚本的单次使用语义保存测试验证码 HMAC。 */
class FakeRedis {
  private readonly values = new Map<string, string>();

  /** 持久化测试挑战摘要。 */
  public set(key: string, value: string): Promise<void> {
    this.values.set(key, value);
    return Promise.resolve();
  }

  /** 比较候选答案前删除挑战，确保错误答案形成终态。 */
  public consumeMatchingValue(key: string, expectedValue: string): Promise<boolean> {
    const value = this.values.get(key);
    this.values.delete(key);
    return Promise.resolve(value === expectedValue);
  }
}

/** 仅在测试进程内通过构造函数注入静态答案。 */
class FixtureCaptchaCodeGenerator implements CaptchaCodeGenerator {
  /** 返回本单元测试预期的非生产 fixture 答案。 */
  public generate(): string {
    return '2468';
  }
}

/** 构造与不透明 refresh secret 持久化表示一致的 SHA-256 摘要。 */
function digest(secret: string): string {
  return createHash('sha256').update(secret).digest('base64url');
}

/** 为不调用验证码或 JWT 协作者的方法构造 AuthService。 */
function minimalAuth(client: object): AuthService {
  return new AuthService(
    { client } as unknown as DatabaseService,
    {} as UserService,
    {} as CaptchaService,
    {} as never,
    configuration,
    {} as SecurityRateLimitService,
  );
}
