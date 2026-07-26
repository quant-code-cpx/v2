import { HttpStatus, Injectable, UnauthorizedException } from '@nestjs/common';
import { JwtService } from '@nestjs/jwt';
import { createHash, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';

import { UserStatus } from '../../generated/prisma/client.js';
import { AppConfigService } from '../../platform/config/app-config.service.js';
import type { AuthContext } from '../../platform/http/auth-context.js';
import { PublicProblemException } from '../../platform/http/problem.exception.js';
import { DatabaseService } from '../../platform/database/database.service.js';
import { UserService, normalizeAccount } from '../user/user.service.js';
import type { AuthenticatedUser } from '../user/user.types.js';
import type { CaptchaClientContext } from './captcha.service.js';
import { CaptchaService } from './captcha.service.js';
import type { JwtPayload, TokenPair } from './auth.types.js';
import { SecurityRateLimitService } from './security-rate-limit.service.js';

@Injectable()
export class AuthService {
  /** Compose authentication workflows from user authority, CAPTCHA, session persistence, and Redis controls. */
  public constructor(
    private readonly database: DatabaseService,
    private readonly users: UserService,
    private readonly captcha: CaptchaService,
    private readonly jwt: JwtService,
    private readonly config: AppConfigService,
    private readonly rateLimit: SecurityRateLimitService,
  ) {}

  /** Consume CAPTCHA first, then authenticate account credentials and create a browser session. */
  public async login(
    account: string,
    password: string,
    captchaId: string,
    captchaAnswer: string,
    ip: string,
    captchaContext: CaptchaClientContext,
    requestId?: string,
  ): Promise<TokenPair> {
    // Every submit consumes the challenge before credential outcome so answers cannot be replayed or guessed.
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

  /** Rotate one refresh token under a single-winner compare-and-swap transaction. */
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

  /** Idempotently revoke a valid cookie-identified session; malformed or absent cookies stay indistinguishable. */
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

    // Audit only a state change; repeated valid logout requests remain successful no-ops.
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

  /** Revalidate a decoded JWT against current PostgreSQL session and user security state. */
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

  /** Create a refresh-session family, update last login, audit success, then sign matching tokens. */
  private async createSessionAndTokens(
    user: AuthenticatedUser,
    requestId?: string,
  ): Promise<TokenPair> {
    const sessionId = randomUUID();
    const familyId = sessionId;
    const refreshSecret = randomBytes(32).toString('base64url');
    const refreshExpiresAt = new Date(Date.now() + this.config.refreshTokenTtlSeconds * 1_000);
    // Session persistence, audit, and login timestamp must succeed as one durable authentication event.
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

  /** Atomically revoke previous refresh token and create its successor in same session family. */
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

    // Matching revokedAt/hash predicates make parallel rotations a single-winner operation.
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

  /** Distinguish a short browser race from a later replay of a known revoked refresh token. */
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

  /** Test whether a prior token rotation remains inside its intentionally short client retry grace period. */
  private withinRefreshRaceGrace(rotatedAt: Date): boolean {
    return Date.now() - rotatedAt.getTime() <= this.config.refreshRaceGraceSeconds * 1_000;
  }

  /** Revoke one compromised refresh family instead of signing out unrelated user devices. */
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

  /** Sign short access token and bind opaque refresh secret to persisted session ID. */
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

/** Parse only a UUID session ID followed by non-empty opaque refresh secret. */
function parseRefreshToken(value: string): { sessionId: string; secret: string } | null {
  const separator = value.indexOf('.');
  if (separator <= 0 || separator === value.length - 1) {
    return null;
  }
  const sessionId = value.slice(0, separator);
  return isUuid(sessionId) ? { sessionId, secret: value.slice(separator + 1) } : null;
}

/** Produce storage-safe SHA-256 digest for an opaque refresh secret. */
function hashSecret(secret: string): string {
  return createHash('sha256').update(secret).digest('base64url');
}

/** Compare refresh-secret digests without leaking equality through timing. */
function safeTokenHashEquals(expectedHash: string, secret: string): boolean {
  const expected = Buffer.from(expectedHash);
  const actual = Buffer.from(hashSecret(secret));
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

/** Return uniform 422 CAPTCHA failure for absent, expired, incorrect, consumed, or cross-context challenges. */
function captchaInvalid(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.UNPROCESSABLE_ENTITY,
    'captcha-invalid',
    'Refresh the CAPTCHA image and try again',
  );
}

/** Return uniform credential failure without revealing account status or existence. */
function invalidCredentials(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.UNAUTHORIZED,
    'invalid-credentials',
    'Invalid credentials',
  );
}

/** Return uniform refresh failure without exposing session lifecycle or token replay facts. */
function invalidRefreshToken(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.UNAUTHORIZED,
    'invalid-refresh-token',
    'Invalid refresh token',
  );
}

/** Return a retryable concurrent refresh outcome without revoking an otherwise healthy family. */
function refreshRace(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.CONFLICT,
    'refresh-race',
    'Another refresh request is in progress',
    1,
  );
}

/** Reject non-UUID refresh IDs before database lookup. */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
