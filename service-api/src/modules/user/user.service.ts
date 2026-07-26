import {
  BadRequestException,
  ConflictException,
  ForbiddenException,
  HttpStatus,
  Injectable,
  NotFoundException,
  UnauthorizedException,
} from '@nestjs/common';
import * as argon2 from 'argon2';
import { createHash, createHmac, timingSafeEqual } from 'node:crypto';

import { Prisma, Role, UserStatus } from '../../generated/prisma/client.js';
import type { AuthContext } from '../../platform/http/auth-context.js';
import { PublicProblemException } from '../../platform/http/problem.exception.js';
import { DatabaseService } from '../../platform/database/database.service.js';
import { AppConfigService } from '../../platform/config/app-config.service.js';
import type { ChangePasswordDto } from './dto/change-password.dto.js';
import type { CreateUserDto } from './dto/create-user.dto.js';
import type { ListUsersQueryDto } from './dto/list-users-query.dto.js';
import type { ResetPasswordDto } from './dto/reset-password.dto.js';
import type { UpdateProfileDto } from './dto/update-profile.dto.js';
import type { UpdateUserDto } from './dto/update-user.dto.js';
import type {
  AuthenticatedUser,
  CurrentUserResource,
  Permission,
  UserPage,
  UserResource,
} from './user.types.js';

const ACCOUNT_PATTERN = /^[a-z0-9][a-z0-9._-]{4,31}$/;
const PASSWORD_OPTIONS = {
  type: argon2.argon2id,
  memoryCost: 19_456,
  timeCost: 2,
  parallelism: 1,
} as const;

type AuditContext = {
  actorId: string | null;
  requestId?: string | undefined;
};

type CursorPayload = {
  fingerprint: string;
  id: string;
  signature: string;
};

type AuthorizationSnapshot = {
  id: string;
  role: Role;
  securityVersion: number;
  status: UserStatus;
};

/** 表示初始化任务是否新建了唯一超级管理员，供启动脚本只记录必要状态。 */
type BootstrapSuperAdminResult = {
  created: boolean;
  user: UserResource;
};

@Injectable()
export class UserService {
  /** Provide user lifecycle and target-policy operations through PostgreSQL authority. */
  public constructor(
    private readonly database: DatabaseService,
    private readonly config: AppConfigService,
  ) {}

  /** Verify custom-account credentials while equalizing unknown-account hash work. */
  public async authenticate(account: string, password: string): Promise<AuthenticatedUser | null> {
    const normalizedAccount = normalizeAccount(account);
    const user = await this.database.client.user.findUnique({
      where: { normalizedAccount },
      include: { credential: true },
    });

    if (!user?.credential) {
      // Keep absence comparable to a real Argon2 verification to reduce account-enumeration timing.
      await hashPassword(password);
      return null;
    }

    const passwordMatches = await argon2.verify(user.credential.passwordHash, password);
    return passwordMatches ? this.toAuthenticatedUser(user) : null;
  }

  /** Read mutable authorization fields needed for access-token and refresh-session validation. */
  public async getAuthenticationSnapshot(userId: string): Promise<AuthenticatedUser | null> {
    const user = await this.database.client.user.findUnique({ where: { id: userId } });
    return user ? this.toAuthenticatedUser(user) : null;
  }

  /** Record a successful login timestamp without exposing authentication material. */
  public async markLogin(userId: string): Promise<void> {
    await this.database.client.user.update({
      where: { id: userId },
      data: { lastLoginAt: new Date() },
    });
  }

  /** Return authenticated user's profile with server-derived effective permissions. */
  public async getMe(userId: string): Promise<CurrentUserResource> {
    const user = await this.database.client.user.findUnique({ where: { id: userId } });
    if (!user || user.status !== UserStatus.ACTIVE) {
      throw new UnauthorizedException();
    }
    return { ...this.toResource(user), permissions: permissionsForRole(user.role) };
  }

  /** Update self profile under strong ETag concurrency control and append an audit record. */
  public async updateMe(
    actor: AuthContext,
    input: UpdateProfileDto,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<CurrentUserResource> {
    const displayName = normalizeDisplayName(input.displayName);
    assertValidDisplayName(displayName);
    // Mutation and audit evidence commit together; version predicate prevents a stale overwrite.
    const user = await this.database.client.$transaction(async (transaction) => {
      const current = await transaction.user.findUnique({ where: { id: actor.userId } });
      this.assertCurrentActor(actor, current);
      this.assertExpectedVersion(current.version, expectedVersion);
      const update = await transaction.user.updateMany({
        where: { id: actor.userId, version: expectedVersion },
        data: { displayName, version: { increment: 1 } },
      });
      if (update.count !== 1) {
        throw preconditionFailed();
      }
      const updated = await transaction.user.findUniqueOrThrow({ where: { id: actor.userId } });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.profile.updated',
          targetId: actor.userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
        },
      });
      return updated;
    });
    return { ...this.toResource(user), permissions: permissionsForRole(user.role) };
  }

  /** Verify current password, revoke old sessions, increment security version, and audit atomically. */
  public async changePassword(
    actor: AuthContext,
    input: ChangePasswordDto,
    context: AuditContext,
  ): Promise<void> {
    const passwordHash = await hashPassword(input.newPassword);
    // Password hash verification runs inside the write transaction so successful verification cannot race a reset.
    await this.database.client.$transaction(async (transaction) => {
      const current = await transaction.user.findUnique({ where: { id: actor.userId } });
      this.assertCurrentActor(actor, current);
      const credential = await transaction.credential.findUnique({
        where: { userId: actor.userId },
      });
      if (!credential || !(await argon2.verify(credential.passwordHash, input.currentPassword))) {
        throw new UnauthorizedException('Current password is invalid');
      }
      await transaction.credential.update({
        where: { userId: actor.userId },
        data: { passwordHash, passwordChangedAt: new Date() },
      });
      await transaction.user.update({
        where: { id: actor.userId },
        data: { securityVersion: { increment: 1 }, version: { increment: 1 } },
      });
      await transaction.session.updateMany({
        where: { userId: actor.userId, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.password.changed',
          targetId: actor.userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
        },
      });
    });
  }

  /** List manageable targets plus the actor's own read-only identity using signed cursor filters. */
  public async listUsers(actor: AuthContext, query: ListUsersQueryDto): Promise<UserPage> {
    this.assertManager(actor);
    if (query.status === UserStatus.DELETED && actor.role !== Role.SUPER_ADMIN) {
      throw new ForbiddenException('Deleted users require super-administrator authority');
    }

    const fingerprint = this.cursorFingerprint(actor, query);
    const cursorId =
      query.cursor === undefined ? undefined : this.parseCursor(query.cursor, fingerprint);
    const managedRoles = manageableRoles(actor.role);
    const searchQuery = query.q?.trim();
    const normalizedQuery = searchQuery?.toLowerCase();
    const requestedRoles =
      query.role === undefined
        ? managedRoles
        : managedRoles.includes(query.role)
          ? [query.role]
          : [];
    const includeActor = query.role === undefined || query.role === actor.role;
    const roleScope: Prisma.UserWhereInput = includeActor
      ? { OR: [{ role: { in: requestedRoles } }, { id: actor.userId }] }
      : { role: { in: requestedRoles } };
    const where: Prisma.UserWhereInput = {
      AND: [
        roleScope,
        { status: query.status ?? { not: UserStatus.DELETED } },
        ...(normalizedQuery && searchQuery
          ? [
              {
                OR: [
                  { normalizedAccount: { contains: normalizedQuery } },
                  { displayName: { contains: searchQuery, mode: 'insensitive' as const } },
                ],
              },
            ]
          : []),
      ],
    };
    const orderField = query.sort === 'account' ? 'normalizedAccount' : query.sort;
    const orderBy = [
      { [orderField]: query.order },
      { id: query.order },
    ] as Prisma.UserOrderByWithRelationInput[];

    // Fetch one extra row to derive a deterministic next cursor without an extra count query.
    const users = await this.database.client.user.findMany({
      where,
      orderBy,
      take: query.pageSize + 1,
      ...(cursorId === undefined ? {} : { cursor: { id: cursorId }, skip: 1 }),
    });
    const hasMore = users.length > query.pageSize;
    const pageUsers = hasMore ? users.slice(0, query.pageSize) : users;
    const last = pageUsers.at(-1);
    return {
      items: pageUsers.map((user) => this.toResource(user)),
      page: { nextCursor: hasMore && last ? this.createCursor(last.id, fingerprint) : null },
    };
  }

  /** Read a non-deleted target only after target-level administrator scope is checked. */
  public async getUser(actor: AuthContext, userId: string): Promise<UserResource> {
    this.assertManager(actor);
    const target = await this.database.client.user.findUnique({ where: { id: userId } });
    this.assertTargetVisible(actor, target);
    return this.toResource(target);
  }

  /** Create only a role permitted to the current administrator and append an audit record. */
  public async createUser(
    actor: AuthContext,
    input: CreateUserDto,
    context: AuditContext,
  ): Promise<UserResource> {
    const account = normalizeAccount(input.account);
    assertValidAccount(account);
    const displayName = normalizeDisplayName(input.displayName);
    assertValidDisplayName(displayName);
    const requestedRole = input.role ?? Role.USER;
    const requestedStatus = input.status ?? UserStatus.ACTIVE;
    this.assertRequestedRole(actor, requestedRole);
    if (requestedStatus === UserStatus.DELETED) {
      throw new BadRequestException('DELETED is not a creatable status');
    }
    const passwordHash = await hashPassword(input.password);

    try {
      // Recheck actor in same transaction so an out-of-date access token cannot create a target.
      const user = await this.database.client.$transaction(async (transaction) => {
        const currentActor = await transaction.user.findUnique({ where: { id: actor.userId } });
        this.assertCurrentActor(actor, currentActor);
        this.assertRequestedRole(actor, requestedRole);
        const created = await transaction.user.create({
          data: {
            account,
            normalizedAccount: account,
            displayName,
            role: requestedRole,
            status: requestedStatus,
            credential: { create: { passwordHash, passwordChangedAt: new Date() } },
          },
        });
        await transaction.auditLog.create({
          data: {
            actorId: context.actorId,
            action: 'user.created',
            targetId: created.id,
            ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
            metadata: { actorRole: actor.role, account: maskAccount(account), role: requestedRole },
          },
        });
        return created;
      });
      return this.toResource(user);
    } catch (error: unknown) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2002') {
        throw new ConflictException('Account is already in use');
      }
      throw error;
    }
  }

  /** Update a managed target with ETag protection, session invalidation, and auditable state change. */
  public async updateUser(
    actor: AuthContext,
    userId: string,
    input: UpdateUserDto,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<UserResource> {
    if (input.displayName === undefined && input.role === undefined && input.status === undefined) {
      throw new BadRequestException('At least one field is required');
    }
    if (input.role === Role.SUPER_ADMIN || input.status === UserStatus.DELETED) {
      throw new BadRequestException('Requested role or status is not mutable');
    }
    const displayName =
      input.displayName === undefined ? undefined : normalizeDisplayName(input.displayName);
    if (displayName !== undefined) {
      assertValidDisplayName(displayName);
    }

    // Update, session revocation, and audit record form one security-state transaction.
    const user = await this.database.client.$transaction(async (transaction) => {
      const currentActor = await transaction.user.findUnique({ where: { id: actor.userId } });
      this.assertCurrentActor(actor, currentActor);
      const target = await transaction.user.findUnique({ where: { id: userId } });
      this.assertTargetVisible(actor, target);
      this.assertExpectedVersion(target.version, expectedVersion);
      if (input.role !== undefined) {
        this.assertRequestedRole(actor, input.role);
      }
      const securityChanged =
        (input.role !== undefined && input.role !== target.role) ||
        (input.status !== undefined && input.status !== target.status);
      const update = await transaction.user.updateMany({
        where: { id: userId, version: expectedVersion },
        data: {
          ...(displayName === undefined ? {} : { displayName }),
          ...(input.role === undefined ? {} : { role: input.role }),
          ...(input.status === undefined ? {} : { status: input.status }),
          version: { increment: 1 },
          ...(securityChanged ? { securityVersion: { increment: 1 } } : {}),
        },
      });
      if (update.count !== 1) {
        throw preconditionFailed();
      }
      if (securityChanged) {
        await transaction.session.updateMany({
          where: { userId, revokedAt: null },
          data: { revokedAt: new Date() },
        });
      }
      const updated = await transaction.user.findUniqueOrThrow({ where: { id: userId } });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.admin.updated',
          targetId: userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
          metadata: {
            actorRole: actor.role,
            before: { role: target.role, status: target.status },
            after: { role: updated.role, status: updated.status },
          },
        },
      });
      return updated;
    });
    return this.toResource(user);
  }

  /** Soft-delete a target, invalidate every session, and keep repeat deletion idempotent. */
  public async deleteUser(
    actor: AuthContext,
    userId: string,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<void> {
    // Deleted target terminal state intentionally wins over a stale ETag for repeat DELETE requests.
    await this.database.client.$transaction(async (transaction) => {
      const currentActor = await transaction.user.findUnique({ where: { id: actor.userId } });
      this.assertCurrentActor(actor, currentActor);
      const target = await transaction.user.findUnique({ where: { id: userId } });
      this.assertTargetManageable(actor, target, true);
      if (target.status === UserStatus.DELETED) {
        return;
      }
      this.assertExpectedVersion(target.version, expectedVersion);
      const update = await transaction.user.updateMany({
        where: { id: userId, version: expectedVersion },
        data: {
          status: UserStatus.DELETED,
          deletedAt: new Date(),
          version: { increment: 1 },
          securityVersion: { increment: 1 },
        },
      });
      if (update.count !== 1) {
        throw preconditionFailed();
      }
      await transaction.session.updateMany({
        where: { userId, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.deleted',
          targetId: userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
          metadata: { actorRole: actor.role, account: maskAccount(target.account) },
        },
      });
    });
  }

  /** Replace a managed target password without returning plaintext or retaining credential material. */
  public async resetPassword(
    actor: AuthContext,
    userId: string,
    input: ResetPasswordDto,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<void> {
    const passwordHash = await hashPassword(input.password);
    // Credential, security version, session revocation, and audit must share a single commit boundary.
    await this.database.client.$transaction(async (transaction) => {
      const currentActor = await transaction.user.findUnique({ where: { id: actor.userId } });
      this.assertCurrentActor(actor, currentActor);
      const target = await transaction.user.findUnique({ where: { id: userId } });
      this.assertTargetVisible(actor, target);
      this.assertExpectedVersion(target.version, expectedVersion);
      const update = await transaction.user.updateMany({
        where: { id: userId, version: expectedVersion },
        data: { securityVersion: { increment: 1 }, version: { increment: 1 } },
      });
      if (update.count !== 1) {
        throw preconditionFailed();
      }
      await transaction.credential.update({
        where: { userId },
        data: { passwordHash, passwordChangedAt: new Date() },
      });
      await transaction.session.updateMany({
        where: { userId, revokedAt: null },
        data: { revokedAt: new Date() },
      });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.password.reset',
          targetId: userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
          metadata: { actorRole: actor.role, account: maskAccount(target.account) },
        },
      });
    });
  }

  /**
   * 在空用户库中创建唯一 `SUPER_ADMIN`，或确认已有活动超级管理员而不改写其任何安全状态。
   *
   * 所有判定都在同一 PostgreSQL 事务和 advisory lock 内完成，避免并发启动造成越权、覆盖或重复审计。
   */
  public async ensureBootstrapSuperAdmin(
    accountInput: string | undefined,
    password: string | undefined,
  ): Promise<BootstrapSuperAdminResult> {
    const account = accountInput === undefined ? undefined : normalizeAccount(accountInput);

    try {
      return await this.database.client.$transaction(
        async (transaction) => {
          await transaction.$executeRaw(
            Prisma.sql`SELECT pg_advisory_xact_lock(hashtext('apex-super-admin-bootstrap'))`,
          );
          const existingSuperAdmin = await transaction.user.findFirst({
            where: { role: Role.SUPER_ADMIN },
          });
          if (existingSuperAdmin?.status === UserStatus.ACTIVE) {
            return { created: false, user: this.toResource(existingSuperAdmin) };
          }
          if (existingSuperAdmin) {
            // 禁用或删除的超级管理员必须由受控恢复流程处理，启动任务不能借机替换权限主体。
            throw new ConflictException(
              'Refusing bootstrap: existing super administrator is not active',
            );
          }
          if ((await transaction.user.count()) > 0) {
            // 既有用户库需要显式运维流程；自动初始化绝不把现有账号提升为超级管理员。
            throw new ConflictException('Refusing bootstrap: users already exist');
          }
          if (!account || !password) {
            throw new BadRequestException(
              'BOOTSTRAP_ADMIN_ACCOUNT and BOOTSTRAP_ADMIN_PASSWORD are required when no super administrator exists',
            );
          }
          assertValidAccount(account);

          const passwordHash = await hashPassword(password);
          const created = await transaction.user.create({
            data: {
              account,
              normalizedAccount: account,
              displayName: 'Super Administrator',
              role: Role.SUPER_ADMIN,
              status: UserStatus.ACTIVE,
              credential: { create: { passwordHash, passwordChangedAt: new Date() } },
            },
          });
          await transaction.auditLog.create({
            data: {
              actorId: null,
              action: 'system.bootstrap.super_admin_created',
              targetId: created.id,
              metadata: { account: maskAccount(account) },
            },
          });
          return { created: true, user: this.toResource(created) };
        },
        { isolationLevel: Prisma.TransactionIsolationLevel.Serializable },
      );
    } catch (error: unknown) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2002') {
        // 唯一索引竞争只能安全失败；重试时会重新判定是否已有活动超级管理员。
        throw new ConflictException(
          'Refusing bootstrap: account or super administrator changed concurrently',
        );
      }
      throw error;
    }
  }

  /** Promote one explicitly named active legacy administrator exactly once outside ordinary HTTP APIs. */
  public async promoteExistingAdminToSuperAdmin(accountInput: string): Promise<UserResource> {
    const account = normalizeAccount(accountInput);
    assertValidAccount(account);

    // A database lock plus the partial unique index makes the operational promotion single-winner.
    const user = await this.database.client.$transaction(
      async (transaction) => {
        await transaction.$executeRaw(
          Prisma.sql`SELECT pg_advisory_xact_lock(hashtext('apex-super-admin-bootstrap'))`,
        );
        const existing = await transaction.user.findFirst({
          where: { role: Role.SUPER_ADMIN },
          select: { id: true },
        });
        if (existing) {
          throw new ConflictException('Refusing promotion: a super administrator already exists');
        }

        const target = await transaction.user.findUnique({ where: { normalizedAccount: account } });
        if (!target || target.role !== Role.ADMIN || target.status !== UserStatus.ACTIVE) {
          // Do not disclose whether an account exists or why it cannot be promoted to an operator.
          throw new BadRequestException(
            'Promotion requires one explicitly named active ADMIN account',
          );
        }
        const update = await transaction.user.updateMany({
          where: { id: target.id, role: Role.ADMIN, status: UserStatus.ACTIVE },
          data: {
            role: Role.SUPER_ADMIN,
            securityVersion: { increment: 1 },
            version: { increment: 1 },
          },
        });
        if (update.count !== 1) {
          throw new ConflictException('Administrator changed during promotion');
        }
        await transaction.session.updateMany({
          where: { userId: target.id, revokedAt: null },
          data: { revokedAt: new Date() },
        });
        await transaction.auditLog.create({
          data: {
            actorId: null,
            action: 'system.bootstrap.super_admin_promoted',
            targetId: target.id,
            metadata: { account: maskAccount(account) },
          },
        });
        return transaction.user.findUniqueOrThrow({ where: { id: target.id } });
      },
      { isolationLevel: Prisma.TransactionIsolationLevel.Serializable },
    );
    return this.toResource(user);
  }

  /** Report whether a database already has an identity, for operational bootstrap diagnostics. */
  public async hasUsers(): Promise<boolean> {
    return (await this.database.client.user.count()) > 0;
  }

  /** Reject stale, disabled, deleted, role-changed, or security-version-changed actor state. */
  private assertCurrentActor(
    actor: AuthContext,
    current: AuthorizationSnapshot | null,
  ): asserts current is AuthorizationSnapshot {
    if (
      !current ||
      current.status !== UserStatus.ACTIVE ||
      current.role !== actor.role ||
      current.securityVersion !== actor.securityVersion
    ) {
      throw new UnauthorizedException();
    }
  }

  /** Require an administrator before exposing target management operations. */
  private assertManager(actor: AuthContext): void {
    if (actor.role !== Role.ADMIN && actor.role !== Role.SUPER_ADMIN) {
      throw new ForbiddenException('Administrator authority required');
    }
  }

  /** Enforce create/role-change hierarchy without offering any ordinary SUPER_ADMIN mutation path. */
  private assertRequestedRole(actor: AuthContext, requestedRole: Role): void {
    this.assertManager(actor);
    if (requestedRole === Role.SUPER_ADMIN) {
      throw new ForbiddenException('SUPER_ADMIN cannot be granted through this API');
    }
    if (actor.role === Role.ADMIN && requestedRole !== Role.USER) {
      throw new ForbiddenException('Administrators may create or assign USER only');
    }
  }

  /** Return target detail only when actor has management authority and target is not deleted. */
  private assertTargetVisible<T extends AuthorizationSnapshot & { version: number }>(
    actor: AuthContext,
    target: T | null,
  ): asserts target is T {
    this.assertTargetManageable(actor, target, false);
  }

  /** Apply hidden target scope, self-operation ban, and optional deleted-terminal-state allowance. */
  private assertTargetManageable<T extends AuthorizationSnapshot & { version: number }>(
    actor: AuthContext,
    target: T | null,
    allowDeleted: boolean,
  ): asserts target is T {
    this.assertManager(actor);
    if (
      !target ||
      target.id === actor.userId ||
      target.role === Role.SUPER_ADMIN ||
      (actor.role === Role.ADMIN && target.role !== Role.USER) ||
      (!allowDeleted && target.status === UserStatus.DELETED)
    ) {
      // Return 404 for inaccessible targets to avoid disclosing administrator or deleted identity data.
      throw new NotFoundException('User not found');
    }
  }

  /** Compare resource version supplied by strong ETag with currently stored version. */
  private assertExpectedVersion(actual: number, expected: number): void {
    if (actual !== expected) {
      throw preconditionFailed();
    }
  }

  /** Bind cursor to actor-visible filters so copied or tampered cursors cannot alter query scope. */
  private cursorFingerprint(actor: AuthContext, query: ListUsersQueryDto): string {
    const values = JSON.stringify({
      actorId: actor.userId,
      order: query.order,
      q: query.q?.trim().toLowerCase() ?? null,
      role: query.role ?? null,
      sort: query.sort,
      status: query.status ?? null,
    });
    return createHash('sha256').update(values).digest('base64url');
  }

  /** Encode only ID and filter fingerprint into a signed opaque cursor. */
  private createCursor(id: string, fingerprint: string): string {
    const unsigned = JSON.stringify({ id, fingerprint });
    const signature = createHmac('sha256', this.config.jwtAccessSecret)
      .update(unsigned)
      .digest('base64url');
    return Buffer.from(
      JSON.stringify({ id, fingerprint, signature } satisfies CursorPayload),
    ).toString('base64url');
  }

  /** Verify signed cursor belongs to identical query context before using its database ID. */
  private parseCursor(cursor: string, expectedFingerprint: string): string {
    try {
      const payload = JSON.parse(
        Buffer.from(cursor, 'base64url').toString('utf8'),
      ) as CursorPayload;
      const unsigned = JSON.stringify({ id: payload.id, fingerprint: payload.fingerprint });
      const expectedSignature = createHmac('sha256', this.config.jwtAccessSecret)
        .update(unsigned)
        .digest('base64url');
      const expected = Buffer.from(expectedSignature);
      const actual = Buffer.from(payload.signature ?? '');
      if (
        !isUuid(payload.id) ||
        payload.fingerprint !== expectedFingerprint ||
        actual.length !== expected.length ||
        !timingSafeEqual(actual, expected)
      ) {
        throw new Error('invalid cursor');
      }
      return payload.id;
    } catch {
      throw new BadRequestException('Invalid cursor');
    }
  }

  /** Project stored user record into public API resource while excluding every credential field. */
  private toResource(user: {
    account: string;
    createdAt: Date;
    deletedAt: Date | null;
    displayName: string;
    id: string;
    lastLoginAt: Date | null;
    role: Role;
    status: UserStatus;
    updatedAt: Date;
    version: number;
  }): UserResource {
    return {
      id: user.id,
      account: user.account,
      displayName: user.displayName,
      role: user.role,
      status: user.status,
      version: user.version,
      lastLoginAt: user.lastLoginAt?.toISOString() ?? null,
      deletedAt: user.deletedAt?.toISOString() ?? null,
      createdAt: user.createdAt.toISOString(),
      updatedAt: user.updatedAt.toISOString(),
    };
  }

  /** Project database record into minimal mutable authorization snapshot. */
  private toAuthenticatedUser(user: {
    account: string;
    displayName: string;
    id: string;
    role: Role;
    securityVersion: number;
    status: UserStatus;
  }): AuthenticatedUser {
    return {
      id: user.id,
      account: user.account,
      displayName: user.displayName,
      role: user.role,
      status: user.status,
      securityVersion: user.securityVersion,
    };
  }
}

/** Normalize account lookup input without applying creation-format validation to login attempts. */
export function normalizeAccount(account: string): string {
  return account.trim().toLowerCase();
}

/** Validate account syntax only for bootstrap and administrator-created identities. */
export function assertValidAccount(account: string): void {
  if (!ACCOUNT_PATTERN.test(account)) {
    throw new BadRequestException(
      'Account must use 5–32 lowercase letters, numbers, dots, underscores, or hyphens',
    );
  }
}

/** Trim a display name at the service boundary so non-HTTP callers cannot persist surrounding whitespace. */
function normalizeDisplayName(displayName: unknown): string {
  if (typeof displayName !== 'string') {
    throw new BadRequestException('Display name must be a string');
  }
  return displayName.trim();
}

/** Reject empty or oversized names even when a caller bypasses HTTP DTO validation. */
function assertValidDisplayName(displayName: string): void {
  if (displayName.length < 1 || displayName.length > 120) {
    throw new BadRequestException('Display name must contain 1–120 characters');
  }
}

/** Return role inheritance as a fixed server-owned permission list for UI capability display. */
export function permissionsForRole(role: Role): Permission[] {
  const self: Permission[] = ['profile:read', 'profile:update', 'password:change'];
  if (role === Role.USER) {
    return self;
  }
  const administrator: Permission[] = [
    'users:read',
    'users:create',
    'users:update',
    'users:delete',
    'users:reset-password',
  ];
  if (role === Role.ADMIN) {
    return [...self, ...administrator];
  }
  return [...self, ...administrator, 'admins:create', 'admins:manage'];
}

/** Return roles manageable by an administrator; actor self-visibility is added separately by ID. */
function manageableRoles(role: Role): Role[] {
  return role === Role.SUPER_ADMIN ? [Role.USER, Role.ADMIN] : [Role.USER];
}

/** Hash passwords with Argon2id parameters shared by bootstrap, creation, reset, and self-service flows. */
async function hashPassword(password: string): Promise<string> {
  return argon2.hash(password, PASSWORD_OPTIONS);
}

/** Build a stable 412 response for ETag mismatch without leaking the current resource representation. */
function preconditionFailed(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.PRECONDITION_FAILED,
    'precondition-failed',
    'User changed since it was loaded',
  );
}

/** Identify UUID cursor IDs before passing them into Prisma cursor lookup. */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

/** Mask account values before audit metadata persists an identifier useful for forensic correlation. */
function maskAccount(account: string): string {
  return `${account.slice(0, 2)}***${account.slice(-2)}`;
}
