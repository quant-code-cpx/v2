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
import type { AuthContext } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { DatabaseService } from '../../shared/database/database.service.js';
import { AppConfigService } from '../../config/app-config.service.js';
import type { ChangePasswordDto } from './dto/change-password.dto.js';
import type { CreateUserDto } from './dto/create-user.dto.js';
import type { ListUsersQueryDto } from './dto/list-users-query.dto.js';
import type { ResetPasswordDto } from './dto/reset-password.dto.js';
import type { UpdateProfileDto } from './dto/update-profile.dto.js';
import type { UpdateUserDto } from './dto/update-user.dto.js';
import type {
  AuthenticatedUser,
  CurrentUserResource,
  ManageableUserStatistics,
  Permission,
  RoleStatistics,
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
  /** 通过 PostgreSQL 权威状态提供用户生命周期与目标级策略操作。 */
  public constructor(
    private readonly database: DatabaseService,
    private readonly config: AppConfigService,
  ) {}

  /** 校验自定义账号凭据，并对未知账号执行等量 hash 工作。 */
  public async authenticate(account: string, password: string): Promise<AuthenticatedUser | null> {
    const normalizedAccount = normalizeAccount(account);
    const user = await this.database.client.user.findUnique({
      where: { normalizedAccount },
      include: { credential: true },
    });

    if (!user?.credential) {
      // 让账号不存在与真实 Argon2 校验耗时接近，降低账号枚举风险。
      await hashPassword(password);
      return null;
    }

    const passwordMatches = await argon2.verify(user.credential.passwordHash, password);
    return passwordMatches ? this.toAuthenticatedUser(user) : null;
  }

  /** 读取 access token 与 refresh Session 校验需要的可变授权字段。 */
  public async getAuthenticationSnapshot(userId: string): Promise<AuthenticatedUser | null> {
    const user = await this.database.client.user.findUnique({ where: { id: userId } });
    return user ? this.toAuthenticatedUser(user) : null;
  }

  /** 记录成功登录时间，且不暴露认证材料。 */
  public async markLogin(userId: string): Promise<void> {
    await this.database.client.user.update({
      where: { id: userId },
      data: { lastLoginAt: new Date() },
    });
  }

  /** 返回认证用户资料及服务端计算的有效权限。 */
  public async getMe(userId: string): Promise<CurrentUserResource> {
    const user = await this.database.client.user.findUnique({ where: { id: userId } });
    if (!user || user.status !== UserStatus.ACTIVE) {
      throw new UnauthorizedException();
    }
    return { ...this.toResource(user), permissions: permissionsForRole(user.role) };
  }

  /** 在强 ETag 并发控制下更新本人资料并追加审计记录。 */
  public async updateMe(
    actor: AuthContext,
    input: UpdateProfileDto,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<CurrentUserResource> {
    const displayName = normalizeDisplayName(input.displayName);
    assertValidDisplayName(displayName);
    // 变更与审计证据共同提交；版本谓词阻止陈旧写覆盖。
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

  /** 原子校验当前密码、撤销旧 Session、递增安全版本并写入审计。 */
  public async changePassword(
    actor: AuthContext,
    input: ChangePasswordDto,
    context: AuditContext,
  ): Promise<void> {
    const passwordHash = await hashPassword(input.newPassword);
    // 密码 hash 校验在写事务内执行，避免成功校验与并发重置竞态。
    await this.database.client.$transaction(async (transaction) => {
      const current = await transaction.user.findUnique({ where: { id: actor.userId } });
      this.assertCurrentActor(actor, current);
      const credential = await transaction.credential.findUnique({
        where: { userId: actor.userId },
      });
      if (!credential || !(await argon2.verify(credential.passwordHash, input.currentPassword))) {
        throw new PublicProblemException(
          HttpStatus.UNAUTHORIZED,
          'current-password-invalid',
          'Current password is invalid',
        );
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

  /** 使用签名游标筛选可管理目标，并加入调用方本人只读身份。 */
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

    // 多取一行生成确定性下一游标，避免额外 count 查询。
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

  /** 返回严格限定到调用方可管理角色的状态与近三十天登录统计。 */
  public async getManageableStatistics(actor: AuthContext): Promise<ManageableUserStatistics> {
    this.assertManager(actor);
    const generatedAt = new Date();
    const scope = manageableRoles(actor.role) as Array<Exclude<Role, 'SUPER_ADMIN'>>;
    const where: Prisma.UserWhereInput = {
      id: { not: actor.userId },
      role: { in: scope },
    };
    const loggedInSince = new Date(generatedAt.getTime() - 30 * 24 * 60 * 60 * 1_000);

    // 状态分组和近三十天计数在一个可重复读事务中形成同一时刻快照。
    const result = await this.database.client.$transaction(
      async (transaction) => {
        const grouped = await transaction.user.groupBy({
          by: ['role', 'status'],
          where,
          _count: { _all: true },
        });
        const loggedInLast30Days = await transaction.user.count({
          where: {
            ...where,
            lastLoginAt: { gte: loggedInSince },
          },
        });
        return { grouped, loggedInLast30Days };
      },
      // 两条聚合必须看到同一快照，避免状态总数与近三十天登录数来自不同提交点。
      { isolationLevel: Prisma.TransactionIsolationLevel.RepeatableRead },
    );

    // 固定返回完整角色范围和所有状态零值，避免前端从缺失行猜测权限或状态。
    const byRole: RoleStatistics[] = [];
    for (const role of scope) {
      const statistics: RoleStatistics = {
        role,
        total: 0,
        active: 0,
        disabled: 0,
        deleted: 0,
      };
      for (const row of result.grouped) {
        if (row.role !== role) {
          continue;
        }
        const count = row._count._all;
        statistics.total += count;
        if (row.status === UserStatus.ACTIVE) {
          statistics.active += count;
        } else if (row.status === UserStatus.DISABLED) {
          statistics.disabled += count;
        } else {
          statistics.deleted += count;
        }
      }
      byRole.push(statistics);
    }
    const total = { total: 0, active: 0, disabled: 0, deleted: 0 };
    for (const item of byRole) {
      total.total += item.total;
      total.active += item.active;
      total.disabled += item.disabled;
      total.deleted += item.deleted;
    }
    return {
      generatedAt: generatedAt.toISOString(),
      scope,
      ...total,
      loggedInLast30Days: result.loggedInLast30Days,
      byRole,
    };
  }

  /** 完成管理员目标级范围校验后读取未删除目标。 */
  public async getUser(actor: AuthContext, userId: string): Promise<UserResource> {
    this.assertManager(actor);
    const target = await this.database.client.user.findUnique({ where: { id: userId } });
    this.assertTargetVisible(actor, target);
    return this.toResource(target);
  }

  /** 只创建当前管理员允许的角色并追加审计记录。 */
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
      // 在同一事务复验调用方，避免陈旧 access token 创建目标。
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

  /** 使用 ETag 保护更新可管理目标，并同步失效 Session 与记录状态变化。 */
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

    // 更新、Session 撤销与审计记录组成同一安全状态事务。
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

  /** 软删除目标、失效全部 Session，并保持重复删除幂等。 */
  public async deleteUser(
    actor: AuthContext,
    userId: string,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<void> {
    // 已删除终态有意优先于陈旧 ETag，保证重复调用删除动作仍得到稳定结果。
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

  /** 替换可管理目标密码，且不返回明文或保留凭据材料。 */
  public async resetPassword(
    actor: AuthContext,
    userId: string,
    input: ResetPasswordDto,
    expectedVersion: number,
    context: AuditContext,
  ): Promise<void> {
    const passwordHash = await hashPassword(input.password);
    // 凭据、安全版本、Session 撤销与审计必须共享提交边界。
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

  /** 在普通 HTTP API 外将显式指定的活动旧管理员一次性提升。 */
  public async promoteExistingAdminToSuperAdmin(accountInput: string): Promise<UserResource> {
    const account = normalizeAccount(accountInput);
    assertValidAccount(account);

    // 数据库锁与部分唯一索引共同保证运维提升只有一个赢家。
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
          // 不向运维调用方泄露账号是否存在或无法提升的具体原因。
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

  /** 报告数据库是否已有身份，供运维初始化诊断。 */
  public async hasUsers(): Promise<boolean> {
    return (await this.database.client.user.count()) > 0;
  }

  /** 拒绝陈旧、禁用、删除、角色变化或安全版本变化的调用方状态。 */
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

  /** 在暴露目标管理操作前要求管理员角色。 */
  private assertManager(actor: AuthContext): void {
    if (actor.role !== Role.ADMIN && actor.role !== Role.SUPER_ADMIN) {
      throw new ForbiddenException('Administrator authority required');
    }
  }

  /** 强制创建与角色变更层级，且不提供普通 SUPER_ADMIN 变更路径。 */
  private assertRequestedRole(actor: AuthContext, requestedRole: Role): void {
    this.assertManager(actor);
    if (requestedRole === Role.SUPER_ADMIN) {
      throw new ForbiddenException('SUPER_ADMIN cannot be granted through this API');
    }
    if (actor.role === Role.ADMIN && requestedRole !== Role.USER) {
      throw new ForbiddenException('Administrators may create or assign USER only');
    }
  }

  /** 仅在调用方拥有管理权限且目标未删除时返回详情。 */
  private assertTargetVisible<T extends AuthorizationSnapshot & { version: number }>(
    actor: AuthContext,
    target: T | null,
  ): asserts target is T {
    this.assertTargetManageable(actor, target, false);
  }

  /** 应用隐藏目标范围、禁止本人操作及可选删除终态放行。 */
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
      // 不可访问目标统一返回 404，避免泄露管理员或已删除身份数据。
      throw new NotFoundException('User not found');
    }
  }

  /** 比较强 ETag 提供的资源版本与当前持久化版本。 */
  private assertExpectedVersion(actual: number, expected: number): void {
    if (actual !== expected) {
      throw preconditionFailed();
    }
  }

  /** 将游标绑定到调用方可见筛选，防止复制或篡改游标改变查询范围。 */
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

  /** 只把 ID 与筛选指纹编码进签名不透明游标。 */
  private createCursor(id: string, fingerprint: string): string {
    const unsigned = JSON.stringify({ id, fingerprint });
    const signature = createHmac('sha256', this.config.jwtAccessSecret)
      .update(unsigned)
      .digest('base64url');
    return Buffer.from(
      JSON.stringify({ id, fingerprint, signature } satisfies CursorPayload),
    ).toString('base64url');
  }

  /** 使用数据库 ID 前验证签名游标属于完全相同的查询上下文。 */
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

  /** 将持久化用户投影为公开 API 资源，并排除全部凭据字段。 */
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

  /** 将数据库记录投影为最小可变授权快照。 */
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

/** 规范化账号查询输入，不对登录尝试套用创建格式校验。 */
export function normalizeAccount(account: string): string {
  return account.trim().toLowerCase();
}

/** 只对初始化与管理员创建身份校验账号语法。 */
export function assertValidAccount(account: string): void {
  if (!ACCOUNT_PATTERN.test(account)) {
    throw new BadRequestException(
      'Account must use 5–32 lowercase letters, numbers, dots, underscores, or hyphens',
    );
  }
}

/** 在服务边界裁剪显示名，防止非 HTTP 调用方持久化首尾空白。 */
function normalizeDisplayName(displayName: unknown): string {
  if (typeof displayName !== 'string') {
    throw new BadRequestException('Display name must be a string');
  }
  return displayName.trim();
}

/** 即使调用方绕过 HTTP DTO 校验，也拒绝空或超长显示名。 */
function assertValidDisplayName(displayName: string): void {
  if (displayName.length < 1 || displayName.length > 120) {
    throw new BadRequestException('Display name must contain 1–120 characters');
  }
}

/** 以服务端固定权限数组返回角色继承结果，供 UI 展示能力。 */
export function permissionsForRole(role: Role): Permission[] {
  const self: Permission[] = [
    'profile:read',
    'profile:update',
    'password:change',
    'sessions:read',
    'sessions:revoke',
  ];
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
  return [...self, ...administrator, 'admins:create', 'admins:manage', 'audit:read'];
}

/** 返回管理员可管理角色；调用方本人可见性另按 ID 加入。 */
function manageableRoles(role: Role): Role[] {
  return role === Role.SUPER_ADMIN ? [Role.USER, Role.ADMIN] : [Role.USER];
}

/** 使用初始化、创建、重置与自助流程共享的 Argon2id 参数计算密码 hash。 */
async function hashPassword(password: string): Promise<string> {
  return argon2.hash(password, PASSWORD_OPTIONS);
}

/** 为 ETag 不匹配构造稳定 412，且不泄露当前资源表示。 */
function preconditionFailed(): PublicProblemException {
  return new PublicProblemException(
    HttpStatus.PRECONDITION_FAILED,
    'precondition-failed',
    'User changed since it was loaded',
  );
}

/** 在传入 Prisma 游标查询前识别 UUID 游标 ID。 */
function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

/** 审计 metadata 持久化前脱敏账号，同时保留取证关联能力。 */
function maskAccount(account: string): string {
  return `${account.slice(0, 2)}***${account.slice(-2)}`;
}
