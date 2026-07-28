import { HttpStatus, Injectable, NotFoundException, UnauthorizedException } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import { createHash, createHmac, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';

import { Prisma, UserStatus } from '../../generated/prisma/client.js';
import { AppConfigService } from '../../config/app-config.service.js';
import type { AuthContext } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import { UserService, normalizeAccount } from '../user/user.service.js';
import type { AuthenticatedUser } from '../user/user.types.js';
import type { CaptchaClientContext } from './captcha.service.js';
import { CaptchaService } from './captcha.service.js';
import type {
  JwtPayload,
  RevokeOtherSessionsResult,
  SessionFamilyPage,
  TokenPair,
} from './auth.types.js';
import type { SessionListDto } from './dto/session-list.dto.js';
import { SecurityRateLimitService } from './security-rate-limit.service.js';

/** 保存会话族游标的排序键、调用方绑定与签名。 */
type SessionCursorPayload = {
  actorId: string;
  createdAt: string;
  id: string;
  signature: string;
};

@Injectable()
export class AuthService {
  /** 组合用户权威状态、CAPTCHA、Session 持久化与 Redis 安全控制。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly users: UserService,
    private readonly captcha: CaptchaService,
    private readonly jwt: JwtService,
    private readonly config: AppConfigService,
    private readonly rateLimit: SecurityRateLimitService,
  ) {}

  /** 先消费 CAPTCHA，再校验账号凭据并创建浏览器 Session。 */
  public async login(
    account: string,
    password: string,
    captchaId: string,
    captchaAnswer: string,
    ip: string,
    captchaContext: CaptchaClientContext,
    requestId?: string,
  ): Promise<TokenPair> {
    // 每次提交先消费 challenge，避免根据凭据结果反复重放或猜测答案。
    if (!(await this.captcha.verifyAndConsume(captchaId, captchaAnswer, captchaContext))) {
      throw captchaInvalid();
    }

    const normalizedAccount = normalizeAccount(account);
    await this.rateLimit.assertLoginAllowed(normalizedAccount, ip);
    const user = await this.users.authenticate(normalizedAccount, password);
    if (!user || user.status !== UserStatus.ACTIVE) {
      await this.rateLimit.recordFailedLogin(normalizedAccount, ip);
      throw invalidCredentials();
    }

    const tokenPair = await this.createSessionAndTokens(user, requestId);
    await this.rateLimit.resetLoginFailures(normalizedAccount, ip);
    return tokenPair;
  }

  /** 通过单赢家 compare-and-swap 事务轮换一个 refresh token。 */
  public async refresh(
    rawRefreshToken: string | null,
    ip: string,
    requestId?: string,
  ): Promise<TokenPair> {
    if (!rawRefreshToken) {
      throw invalidRefreshToken();
    }
    const parsed = parseRefreshToken(rawRefreshToken);
    if (!parsed) {
      throw invalidRefreshToken();
    }
    await this.rateLimit.assertRefreshAllowed(parsed.sessionId, ip);
    if (await this.rateLimit.isRefreshReplayMarked(parsed.sessionId)) {
      throw invalidRefreshToken();
    }

    const session = await this.database.client.session.findUnique({
      where: { id: parsed.sessionId },
    });
    if (!session || session.expiresAt <= new Date() || session.absoluteExpiresAt <= new Date()) {
      throw invalidRefreshToken();
    }
    if (!safeTokenHashEquals(session.refreshTokenHash, parsed.secret)) {
      throw invalidRefreshToken();
    }

    if (session.revokedAt) {
      await this.handleRevokedRefresh(session, parsed.sessionId);
    }

    const user = await this.users.getAuthenticationSnapshot(session.userId);
    if (
      !user ||
      user.status !== UserStatus.ACTIVE ||
      user.securityVersion !== session.securityVersion
    ) {
      throw invalidRefreshToken();
    }

    const tokenPair = await this.rotateSession(session, user, parsed.secret, requestId);
    return tokenPair;
  }

  /** 幂等撤销有效 cookie 标识的 Session，并让畸形与缺失 cookie 保持不可区分。 */
  public async logout(rawRefreshToken: string | null, requestId?: string): Promise<void> {
    const parsed = rawRefreshToken ? parseRefreshToken(rawRefreshToken) : null;
    if (!parsed) {
      return;
    }
    const session = await this.database.client.session.findUnique({
      where: { id: parsed.sessionId },
    });
    if (!session || !safeTokenHashEquals(session.refreshTokenHash, parsed.secret)) {
      return;
    }

    // 只审计真实状态变化；重复合法退出保持成功 no-op。
    await this.database.client.$transaction(async (transaction) => {
      const revoked = await transaction.session.updateMany({
        where: { id: session.id, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      if (revoked.count === 1) {
        await transaction.auditLog.create({
          data: {
            actorId: session.userId,
            action: 'auth.logout',
            targetId: session.id,
            ...(requestId === undefined ? {} : { requestId }),
          },
        });
      }
    });
  }

  /** 使用 PostgreSQL 当前 Session 与用户安全状态复验已解码 JWT。 */
  public async validateAccessToken(payload: JwtPayload): Promise<AuthContext> {
    const session = await this.database.client.session.findUnique({ where: { id: payload.sid } });
    if (
      !session ||
      session.userId !== payload.sub ||
      session.revokedAt ||
      session.expiresAt <= new Date() ||
      session.absoluteExpiresAt <= new Date() ||
      session.securityVersion !== payload.sv
    ) {
      throw new UnauthorizedException();
    }

    const user = await this.users.getAuthenticationSnapshot(payload.sub);
    if (
      !user ||
      user.status !== UserStatus.ACTIVE ||
      user.securityVersion !== payload.sv ||
      user.role !== payload.role
    ) {
      throw new UnauthorizedException();
    }

    return {
      userId: user.id,
      sessionId: session.id,
      role: user.role,
      securityVersion: user.securityVersion,
    };
  }

  /** 返回当前用户活动且未过期的会话族，并将游标绑定到调用方。 */
  public async listMySessionFamilies(
    actor: AuthContext,
    input: SessionListDto,
  ): Promise<SessionFamilyPage> {
    const now = new Date();
    const cursor =
      input.cursor === undefined ? undefined : this.parseSessionCursor(input.cursor, actor.userId);
    const cursorWhere: Prisma.SessionWhereInput =
      cursor === undefined
        ? {}
        : {
            OR: [
              { createdAt: { lt: cursor.createdAt } },
              { createdAt: cursor.createdAt, id: { lt: cursor.id } },
            ],
          };
    const where: Prisma.SessionWhereInput = {
      userId: actor.userId,
      revokedAt: null,
      expiresAt: { gt: now },
      absoluteExpiresAt: { gt: now },
      ...cursorWhere,
    };

    const [currentSession, sessions, total] = await this.database.client.$transaction([
      this.database.client.session.findUnique({
        where: { id: actor.sessionId },
        select: { familyId: true },
      }),
      this.database.client.session.findMany({
        where,
        orderBy: [{ createdAt: 'desc' }, { id: 'desc' }],
        take: input.pageSize + 1,
        select: {
          id: true,
          familyId: true,
          createdAt: true,
          absoluteExpiresAt: true,
        },
      }),
      this.database.client.session.count({
        where: {
          userId: actor.userId,
          revokedAt: null,
          expiresAt: { gt: now },
          absoluteExpiresAt: { gt: now },
        },
      }),
    ]);
    if (!currentSession) {
      throw new UnauthorizedException();
    }

    // 每次 refresh 都先撤销旧行再创建后继，因此每个活动会话族最多只有一行。
    const hasMore = sessions.length > input.pageSize;
    const pageSessions = hasMore ? sessions.slice(0, input.pageSize) : sessions;
    const last = pageSessions.at(-1);
    // 会话列表只投影公开时间和随机 familyId，不返回认证材料。
    const items = pageSessions.map((session) => ({
      familyId: session.familyId,
      current: session.familyId === currentSession.familyId,
      lastActiveAt: session.createdAt.toISOString(),
      absoluteExpiresAt: session.absoluteExpiresAt.toISOString(),
    }));
    return {
      items,
      page: {
        nextCursor:
          hasMore && last ? this.createSessionCursor(actor.userId, last.createdAt, last.id) : null,
      },
      total,
    };
  }

  /** 仅撤销属于当前用户的指定会话族，并让状态变更与审计证据共同提交。 */
  public async revokeMySessionFamily(
    actor: AuthContext,
    familyId: string,
    requestId?: string,
  ): Promise<boolean> {
    // 资源授权、撤销和审计必须共享事务，任一失败都不能留下部分安全状态。
    return this.database.client.$transaction(async (transaction) => {
      const ownedFamily = await transaction.session.findFirst({
        where: { userId: actor.userId, familyId },
        select: { familyId: true },
      });
      if (!ownedFamily) {
        // 未知与其他用户资源保持相同响应，避免利用 UUID 探测所有权。
        throw new NotFoundException('Session family not found');
      }

      const revoked = await transaction.session.updateMany({
        where: { userId: actor.userId, familyId, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      if (revoked.count > 0) {
        await transaction.auditLog.create({
          data: {
            actorId: actor.userId,
            action: 'auth.session_family.revoked',
            targetId: familyId,
            ...(requestId === undefined ? {} : { requestId }),
          },
        });
      }
      const currentSession = await transaction.session.findUnique({
        where: { id: actor.sessionId },
        select: { familyId: true },
      });
      return currentSession?.familyId === familyId;
    });
  }

  /** 撤销除当前会话族外的全部活动会话族，并返回真正改变终态的族数量。 */
  public async revokeMyOtherSessionFamilies(
    actor: AuthContext,
    requestId?: string,
  ): Promise<RevokeOtherSessionsResult> {
    // 当前 family 判定、其他 family 撤销和聚合审计必须读取同一事务状态。
    return this.database.client.$transaction(async (transaction) => {
      const currentSession = await transaction.session.findUnique({
        where: { id: actor.sessionId },
        select: { familyId: true },
      });
      if (!currentSession) {
        throw new UnauthorizedException();
      }

      const now = new Date();
      const revoked = await transaction.session.updateManyAndReturn({
        where: {
          userId: actor.userId,
          familyId: { not: currentSession.familyId },
          revokedAt: null,
          expiresAt: { gt: now },
          absoluteExpiresAt: { gt: now },
        },
        data: { revokedAt: now },
        select: { familyId: true },
      });
      // `RETURNING` 只包含本事务真正更新的行；按 family 去重后不会被并发撤销或遗留重复行夸大。
      const familyIds = [...new Set(revoked.map((session) => session.familyId))];
      if (familyIds.length === 0) {
        return { revokedFamilyCount: 0 };
      }

      await transaction.auditLog.create({
        data: {
          actorId: actor.userId,
          action: 'auth.session_families.others_revoked',
          targetId: actor.userId,
          ...(requestId === undefined ? {} : { requestId }),
          metadata: { revokedFamilyCount: familyIds.length },
        },
      });
      return { revokedFamilyCount: familyIds.length };
    });
  }

  /** 创建包含用户绑定和稳定倒序键的不可伪造会话游标。 */
  private createSessionCursor(actorId: string, createdAt: Date, id: string): string {
    const unsigned = JSON.stringify({ actorId, createdAt: createdAt.toISOString(), id });
    const signature = createHmac('sha256', this.config.jwtAccessSecret)
      .update(unsigned)
      .digest('base64url');
    return Buffer.from(
      JSON.stringify({
        actorId,
        createdAt: createdAt.toISOString(),
        id,
        signature,
      } satisfies SessionCursorPayload),
    ).toString('base64url');
  }

  /** 验证会话游标签名、调用方绑定和排序键格式。 */
  private parseSessionCursor(value: string, actorId: string): { createdAt: Date; id: string } {
    try {
      const payload = JSON.parse(
        Buffer.from(value, 'base64url').toString('utf8'),
      ) as SessionCursorPayload;
      const createdAt = new Date(payload.createdAt);
      const unsigned = JSON.stringify({
        actorId: payload.actorId,
        createdAt: payload.createdAt,
        id: payload.id,
      });
      const expected = Buffer.from(
        createHmac('sha256', this.config.jwtAccessSecret).update(unsigned).digest('base64url'),
      );
      const actual = Buffer.from(payload.signature ?? '');
      if (
        payload.actorId !== actorId ||
        !isUuid(payload.id) ||
        Number.isNaN(createdAt.getTime()) ||
        actual.length !== expected.length ||
        !timingSafeEqual(actual, expected)
      ) {
        throw new Error('invalid cursor');
      }
      return { createdAt, id: payload.id };
    } catch {
      throw new PublicProblemException(
        HttpStatus.BAD_REQUEST,
        'invalid-cursor',
        'Invalid session cursor',
      );
    }
  }

  /** 创建 refresh Session family、更新最近登录、审计成功并签发匹配 token。 */
  private async createSessionAndTokens(
    user: AuthenticatedUser,
    requestId?: string,
  ): Promise<TokenPair> {
    const sessionId = randomUUID();
    const familyId = sessionId;
    const refreshSecret = randomBytes(32).toString('base64url');
    const refreshExpiresAt = new Date(Date.now() + this.config.refreshTokenTtlSeconds * 1_000);
    // Session 持久化、审计与登录时间必须作为一个持久鉴权事件共同成功。
    await this.database.client.$transaction(async (transaction) => {
      await transaction.session.create({
        data: {
          id: sessionId,
          userId: user.id,
          securityVersion: user.securityVersion,
          refreshTokenHash: hashSecret(refreshSecret),
          familyId,
          expiresAt: refreshExpiresAt,
          absoluteExpiresAt: refreshExpiresAt,
        },
      });
      await transaction.user.update({
        where: { id: user.id },
        data: { lastLoginAt: new Date() },
      });
      await transaction.auditLog.create({
        data: {
          actorId: user.id,
          action: 'auth.login.succeeded',
          targetId: user.id,
          ...(requestId === undefined ? {} : { requestId }),
        },
      });
    });
    return this.signTokenPair(sessionId, user, refreshSecret, refreshExpiresAt);
  }

  /** 原子撤销旧 refresh token，并在同一 Session family 创建后继。 */
  private async rotateSession(
    session: {
      absoluteExpiresAt: Date;
      familyId: string;
      id: string;
      userId: string;
    },
    user: AuthenticatedUser,
    previousSecret: string,
    requestId?: string,
  ): Promise<TokenPair> {
    const refreshSecret = randomBytes(32).toString('base64url');
    const now = new Date();
    const requestedExpiry = new Date(now.getTime() + this.config.refreshTokenTtlSeconds * 1_000);
    const refreshExpiresAt =
      requestedExpiry < session.absoluteExpiresAt ? requestedExpiry : session.absoluteExpiresAt;
    if (refreshExpiresAt <= now) {
      throw invalidRefreshToken();
    }
    const successorId = randomUUID();

    // 同时匹配 revokedAt 与 hash，使并行轮换只有一个赢家。
    const successor = await this.database.client.$transaction(async (transaction) => {
      const revoked = await transaction.session.updateMany({
        where: {
          id: session.id,
          userId: user.id,
          revokedAt: null,
          refreshTokenHash: hashSecret(previousSecret),
        },
        data: { revokedAt: now, rotatedAt: now },
      });
      if (revoked.count !== 1) {
        return null;
      }
      const created = await transaction.session.create({
        data: {
          id: successorId,
          userId: user.id,
          securityVersion: user.securityVersion,
          refreshTokenHash: hashSecret(refreshSecret),
          familyId: session.familyId,
          expiresAt: refreshExpiresAt,
          absoluteExpiresAt: session.absoluteExpiresAt,
          rotatedFromId: session.id,
        },
      });
      await transaction.auditLog.create({
        data: {
          actorId: user.id,
          action: 'auth.refresh.rotated',
          targetId: created.id,
          ...(requestId === undefined ? {} : { requestId }),
        },
      });
      return created;
    });
    if (!successor) {
      const latest = await this.database.client.session.findUnique({ where: { id: session.id } });
      if (latest?.revokedAt && latest.rotatedAt && this.withinRefreshRaceGrace(latest.rotatedAt)) {
        throw refreshRace();
      }
      await this.revokeSessionFamily(session.familyId, user.id, session.id);
      await this.rateLimit.markRefreshReplay(session.id);
      throw invalidRefreshToken();
    }
    return this.signTokenPair(successor.id, user, refreshSecret, refreshExpiresAt);
  }

  /** 区分短暂浏览器竞态与已知撤销 refresh token 的后续重放。 */
  private async handleRevokedRefresh(
    session: { familyId: string; id: string; rotatedAt: Date | null; userId: string },
    sessionId: string,
  ): Promise<never> {
    if (session.rotatedAt && this.withinRefreshRaceGrace(session.rotatedAt)) {
      throw refreshRace();
    }
    await this.revokeSessionFamily(session.familyId, session.userId, sessionId);
    await this.rateLimit.markRefreshReplay(sessionId);
    throw invalidRefreshToken();
  }

  /** 判断前一次 token 轮换是否仍在刻意缩短的客户端重试宽限期。 */
  private withinRefreshRaceGrace(rotatedAt: Date): boolean {
    return Date.now() - rotatedAt.getTime() <= this.config.refreshRaceGraceSeconds * 1_000;
  }

  /** 只撤销受损 refresh family，不退出无关用户设备。 */
  private async revokeSessionFamily(
    familyId: string,
    userId: string,
    sessionId: string,
  ): Promise<void> {
    await this.database.client.$transaction(async (transaction) => {
      await transaction.session.updateMany({
        where: { familyId, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      await transaction.auditLog.create({
        data: {
          actorId: userId,
          action: 'auth.refresh.replay_detected',
          targetId: sessionId,
        },
      });
    });
  }

  /** 签发短期 access token，并把不透明 refresh secret 绑定到持久 Session ID。 */
  private async signTokenPair(
    sessionId: string,
    user: AuthenticatedUser,
    refreshSecret: string,
    refreshExpiresAt: Date,
  ): Promise<TokenPair> {
    const accessToken = await this.jwt.signAsync({
      sub: user.id,
      sid: sessionId,
      role: user.role,
      sv: user.securityVersion,
    } satisfies JwtPayload);
    return {
      accessToken,
      refreshToken: `${sessionId}.${refreshSecret}`,
      refreshExpiresAt,
      user: await this.users.getMe(user.id),
    };
  }
}

/** 只解析 UUID Session ID 与非空不透明 refresh secret 组成的值。 */
function parseRefreshToken(value: string): { sessionId: string; secret: string } | null {
  const separator = value.indexOf('.');
  if (separator <= 0 || separator === value.length - 1) {
    return null;
  }
  const sessionId = value.slice(0, separator);
  return isUuid(sessionId) ? { sessionId, secret: value.slice(separator + 1) } : null;
}

/** 为不透明 refresh secret 生成可安全持久化的 SHA-256 摘要。 */
function hashSecret(secret: string): string {
  return createHash('sha256').update(secret).digest('base64url');
}

/** 比较 refresh secret 摘要且不通过耗时泄漏相等性。 */
function safeTokenHashEquals(expectedHash: string, secret: string): boolean {
  const expected = Buffer.from(expectedHash);
  const actual = Buffer.from(hashSecret(secret));
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

/** 对缺失、过期、错误、已消费或跨上下文 challenge 返回统一 422。 */
function captchaInvalid(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.UNPROCESSABLE_ENTITY,
    'captcha-invalid',
    'Refresh the CAPTCHA image and try again',
  );
}

/** 返回统一凭据失败，避免泄露账号状态或存在性。 */
function invalidCredentials(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.UNAUTHORIZED,
    'invalid-credentials',
    'Invalid credentials',
  );
}

/** 返回统一 refresh 失败，避免泄露 Session 生命周期或 token 重放事实。 */
function invalidRefreshToken(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.UNAUTHORIZED,
    'invalid-refresh-token',
    'Invalid refresh token',
  );
}

/** 返回可重试的并发 refresh 结果，不撤销原本健康的 family。 */
function refreshRace(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.CONFLICT,
    'refresh-race',
    'Another refresh request is in progress',
    1,
  );
}

/** 在数据库查询前拒绝非 UUID refresh ID。 */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
