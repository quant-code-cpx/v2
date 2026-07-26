import { ForbiddenException, Injectable, UnauthorizedException } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';

import { Role, UserStatus } from '../../generated/prisma/client.js';
import { AppConfigService } from '../../platform/config/app-config.service.js';
import { DatabaseService } from '../../platform/database/database.service.js';
import { UserService, normalizeEmail } from '../user/user.service.js';
import type { AuthContext, JwtPayload, TokenPair } from './auth.types.js';
import { SecurityRateLimitService } from './security-rate-limit.service.js';

@Injectable()
export class AuthService {
  /** Compose authentication workflows from persistence, user, token, config, and rate-limit services. */
  public constructor(
    private readonly database: DatabaseService,
    private readonly users: UserService,
    private readonly jwt: JwtService,
    private readonly config: AppConfigService,
    private readonly rateLimit: SecurityRateLimitService,
  ) {}

  /** Verify credentials, create session tokens, then record successful-login side effects. */
  public async login(
    email: string,
    password: string,
    ip: string,
    requestId?: string,
  ): Promise<TokenPair> {
    const normalizedEmail = normalizeEmail(email);
    await this.rateLimit.assertLoginAllowed(normalizedEmail, ip);
    const user = await this.users.authenticate(normalizedEmail, password);
    if (!user || user.status !== UserStatus.ACTIVE) {
      await this.rateLimit.recordFailedLogin(normalizedEmail, ip);
      throw new UnauthorizedException('Invalid email or password');
    }

    const tokenPair = await this.createSessionAndTokens(user.id, user.role, user.securityVersion);
    await Promise.all([
      this.rateLimit.resetLoginFailures(normalizedEmail, ip),
      this.users.markLogin(user.id),
      this.database.client.auditLog.create({
        data: {
          actorId: user.id,
          action: 'auth.login.succeeded',
          targetId: user.id,
          ...(requestId === undefined ? {} : { requestId }),
        },
      }),
    ]);
    return tokenPair;
  }

  /**
   * Validate and atomically rotate a refresh token while detecting reuse of an older token.
   */
  public async refresh(rawRefreshToken: string, ip: string, origin?: string): Promise<TokenPair> {
    this.assertAllowedOrigin(origin);
    const parsed = parseRefreshToken(rawRefreshToken);
    if (!parsed) {
      throw new UnauthorizedException('Invalid refresh token');
    }
    await this.rateLimit.assertRefreshAllowed(parsed.sessionId, ip);
    if (await this.rateLimit.isRefreshReplayMarked(parsed.sessionId)) {
      throw new UnauthorizedException('Refresh token replay detected');
    }

    const session = await this.database.client.session.findUnique({
      where: { id: parsed.sessionId },
    });
    if (!session || session.expiresAt <= new Date()) {
      throw new UnauthorizedException('Invalid refresh token');
    }

    if (!safeTokenHashEquals(session.refreshTokenHash, parsed.secret)) {
      throw new UnauthorizedException('Invalid refresh token');
    }

    // A valid secret for revoked session proves replay; revoke whole family before rejecting it.
    if (session.revokedAt) {
      await this.revokeSessionFamily(session.userId, session.id);
      await this.rateLimit.markRefreshReplay(session.id);
      throw new UnauthorizedException('Refresh token replay detected');
    }

    const user = await this.users.getAuthenticationSnapshot(session.userId);
    if (
      !user ||
      user.status !== UserStatus.ACTIVE ||
      user.securityVersion !== session.securityVersion
    ) {
      throw new UnauthorizedException('Invalid refresh token');
    }

    return this.rotateSession(session.id, user.id, user.role, user.securityVersion, parsed.secret);
  }

  /** Revoke caller session and record audit event as one transaction. */
  public async logout(context: AuthContext, requestId?: string): Promise<void> {
    // Session mutation and audit log must commit together to preserve forensic traceability.
    await this.database.client.$transaction(async (transaction) => {
      await transaction.session.updateMany({
        where: { id: context.sessionId, userId: context.userId, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      await transaction.auditLog.create({
        data: {
          actorId: context.userId,
          action: 'auth.logout',
          targetId: context.sessionId,
          ...(requestId === undefined ? {} : { requestId }),
        },
      });
    });
  }

  /** Revalidate JWT claims against mutable session and user security state. */
  public async validateAccessToken(payload: JwtPayload): Promise<AuthContext> {
    const session = await this.database.client.session.findUnique({ where: { id: payload.sid } });
    if (
      !session ||
      session.userId !== payload.sub ||
      session.revokedAt ||
      session.expiresAt <= new Date() ||
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

    return { userId: user.id, sessionId: session.id, role: user.role };
  }

  /** Create persisted refresh-session state before issuing its matching token pair. */
  private async createSessionAndTokens(
    userId: string,
    role: Role,
    securityVersion: number,
  ): Promise<TokenPair> {
    const secret = randomBytes(32).toString('base64url');
    const refreshExpiresAt = new Date(Date.now() + this.config.refreshTokenTtlSeconds * 1_000);
    const session = await this.database.client.session.create({
      data: {
        userId,
        securityVersion,
        refreshTokenHash: hashSecret(secret),
        expiresAt: refreshExpiresAt,
      },
    });
    return this.signTokenPair(session.id, userId, role, securityVersion, secret, refreshExpiresAt);
  }

  /** Atomically consume current refresh session and create its one-time successor. */
  private async rotateSession(
    previousSessionId: string,
    userId: string,
    role: Role,
    securityVersion: number,
    previousSecret: string,
  ): Promise<TokenPair> {
    const secret = randomBytes(32).toString('base64url');
    const refreshExpiresAt = new Date(Date.now() + this.config.refreshTokenTtlSeconds * 1_000);
    // Hash match in update predicate makes simultaneous refreshes race safely: only one can rotate.
    const newSession = await this.database.client.$transaction(async (transaction) => {
      const revoked = await transaction.session.updateMany({
        where: {
          id: previousSessionId,
          userId,
          revokedAt: null,
          refreshTokenHash: hashSecret(previousSecret),
        },
        data: { revokedAt: new Date() },
      });
      if (revoked.count !== 1) {
        return null;
      }
      return transaction.session.create({
        data: {
          userId,
          securityVersion,
          refreshTokenHash: hashSecret(secret),
          expiresAt: refreshExpiresAt,
          rotatedFromId: previousSessionId,
        },
      });
    });
    if (!newSession) {
      await this.revokeSessionFamily(userId, previousSessionId);
      await this.rateLimit.markRefreshReplay(previousSessionId);
      throw new UnauthorizedException('Refresh token replay detected');
    }
    return this.signTokenPair(
      newSession.id,
      userId,
      role,
      securityVersion,
      secret,
      refreshExpiresAt,
    );
  }

  /** Sign access token and combine session identifier with opaque refresh secret. */
  private async signTokenPair(
    sessionId: string,
    userId: string,
    role: Role,
    securityVersion: number,
    refreshSecret: string,
    refreshExpiresAt: Date,
  ): Promise<TokenPair> {
    const accessToken = await this.jwt.signAsync({
      sub: userId,
      sid: sessionId,
      role,
      sv: securityVersion,
    } satisfies JwtPayload);
    return {
      accessToken,
      refreshToken: `${sessionId}.${refreshSecret}`,
      refreshExpiresAt,
    };
  }

  /** Reject cross-origin refresh attempts that bypass configured browser boundary. */
  private assertAllowedOrigin(origin: string | undefined): void {
    if (origin !== undefined && origin !== this.config.corsOrigin) {
      throw new ForbiddenException('Unexpected refresh origin');
    }
  }

  /** Revoke all active user sessions after confirmed refresh-token replay and audit it. */
  private async revokeSessionFamily(userId: string, sessionId: string): Promise<void> {
    await this.database.client.session.updateMany({
      where: { userId, revokedAt: null },
      data: { revokedAt: new Date() },
    });
    await this.database.client.auditLog.create({
      data: {
        actorId: userId,
        action: 'auth.refresh.replay_detected',
        targetId: sessionId,
      },
    });
  }
}

/** Parse only non-empty `sessionId.secret` refresh-token representation. */
function parseRefreshToken(value: string): { sessionId: string; secret: string } | null {
  const separator = value.indexOf('.');
  if (separator <= 0 || separator === value.length - 1) {
    return null;
  }
  return { sessionId: value.slice(0, separator), secret: value.slice(separator + 1) };
}

/** Produce storage-safe SHA-256 digest for opaque refresh secret. */
function hashSecret(secret: string): string {
  return createHash('sha256').update(secret).digest('base64url');
}

/** Compare refresh-secret digests without leaking equality through timing. */
function safeTokenHashEquals(expectedHash: string, secret: string): boolean {
  const expected = Buffer.from(expectedHash);
  const actual = Buffer.from(hashSecret(secret));
  // `timingSafeEqual` requires equal-length buffers; preserve constant-time comparison otherwise.
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}
