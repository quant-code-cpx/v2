import {
  ConflictException,
  Injectable,
  NotFoundException,
  UnauthorizedException,
} from '@nestjs/common';
import * as argon2 from 'argon2';

import { Prisma, Role, UserStatus } from '../../generated/prisma/client.js';
import { DatabaseService } from '../../platform/database/database.service.js';
import type { ChangePasswordDto } from './dto/change-password.dto.js';
import type { CreateUserDto } from './dto/create-user.dto.js';
import type { ListUsersQueryDto } from './dto/list-users-query.dto.js';
import type { UpdateProfileDto } from './dto/update-profile.dto.js';
import type { UpdateUserDto } from './dto/update-user.dto.js';
import type { AuthenticatedUser, UserPage, UserResource } from './user.types.js';

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

@Injectable()
export class UserService {
  /** Provide user persistence operations through injected database client. */
  public constructor(private readonly database: DatabaseService) {}

  /** Verify normalized email credentials while equalizing nonexistent-user hash work. */
  public async authenticate(email: string, password: string): Promise<AuthenticatedUser | null> {
    const user = await this.database.client.user.findUnique({
      where: { normalizedEmail: normalizeEmail(email) },
      include: { credential: true },
    });

    if (!user?.credential) {
      // Keep absent-user path comparably expensive to avoid email-enumeration timing signal.
      await hashPassword(password);
      return null;
    }

    const passwordMatches = await argon2.verify(user.credential.passwordHash, password);
    if (!passwordMatches) {
      return null;
    }

    return this.toAuthenticatedUser(user);
  }

  /** Read mutable authorization fields needed for token and refresh validation. */
  public async getAuthenticationSnapshot(userId: string): Promise<AuthenticatedUser | null> {
    const user = await this.database.client.user.findUnique({ where: { id: userId } });
    return user ? this.toAuthenticatedUser(user) : null;
  }

  /** Record latest successful-login timestamp. */
  public async markLogin(userId: string): Promise<void> {
    await this.database.client.user.update({
      where: { id: userId },
      data: { lastLoginAt: new Date() },
    });
  }

  /** Return one user's public resource or explicit not-found response. */
  public async getMe(userId: string): Promise<UserResource> {
    const user = await this.database.client.user.findUnique({ where: { id: userId } });
    if (!user) {
      throw new NotFoundException('User not found');
    }
    return this.toResource(user);
  }

  /** Update self-service profile and matching audit record atomically. */
  public async updateMe(
    userId: string,
    input: UpdateProfileDto,
    context: AuditContext,
  ): Promise<UserResource> {
    // Profile mutation and audit evidence must either both persist or both roll back.
    const user = await this.database.client.$transaction(async (transaction) => {
      const updated = await transaction.user.update({
        where: { id: userId },
        data: { displayName: input.displayName.trim(), version: { increment: 1 } },
      });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.profile.updated',
          targetId: userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
        },
      });
      return updated;
    });
    return this.toResource(user);
  }

  /** Verify current password, replace hash, invalidate security state, and audit atomically. */
  public async changePassword(
    userId: string,
    input: ChangePasswordDto,
    context: AuditContext,
  ): Promise<void> {
    const credential = await this.database.client.credential.findUnique({ where: { userId } });
    if (!credential || !(await argon2.verify(credential.passwordHash, input.currentPassword))) {
      throw new UnauthorizedException('Current password is invalid');
    }

    const passwordHash = await hashPassword(input.newPassword);
    // Incrementing securityVersion invalidates every access token and refresh session from earlier state.
    await this.database.client.$transaction(async (transaction) => {
      await transaction.credential.update({
        where: { userId },
        data: { passwordHash, passwordChangedAt: new Date() },
      });
      await transaction.user.update({
        where: { id: userId },
        data: { securityVersion: { increment: 1 }, version: { increment: 1 } },
      });
      await transaction.auditLog.create({
        data: {
          actorId: context.actorId,
          action: 'user.password.changed',
          targetId: userId,
          ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
        },
      });
    });
  }

  /** Fetch stable cursor page of public users. */
  public async listUsers(query: ListUsersQueryDto): Promise<UserPage> {
    // Request one extra row to derive next cursor without a separate count query.
    const users = await this.database.client.user.findMany({
      orderBy: { id: 'asc' },
      take: query.pageSize + 1,
      ...(query.cursor === undefined ? {} : { cursor: { id: query.cursor }, skip: 1 }),
    });
    const hasMore = users.length > query.pageSize;
    const pageUsers = hasMore ? users.slice(0, query.pageSize) : users;
    return {
      items: pageUsers.map((user) => this.toResource(user)),
      page: { nextCursor: hasMore ? (pageUsers.at(-1)?.id ?? null) : null },
    };
  }

  /** Create user credentials and matching audit record, translating duplicate-email constraint. */
  public async createUser(input: CreateUserDto, context: AuditContext): Promise<UserResource> {
    const passwordHash = await hashPassword(input.password);
    try {
      const user = await this.database.client.$transaction(async (transaction) => {
        const created = await transaction.user.create({
          data: {
            email: input.email.trim(),
            normalizedEmail: normalizeEmail(input.email),
            displayName: input.displayName.trim(),
            role: input.role ?? Role.USER,
            status: input.status ?? UserStatus.ACTIVE,
            credential: {
              create: {
                passwordHash,
                passwordChangedAt: new Date(),
              },
            },
          },
        });
        await transaction.auditLog.create({
          data: {
            actorId: context.actorId,
            action: 'user.created',
            targetId: created.id,
            ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
          },
        });
        return created;
      });
      return this.toResource(user);
    } catch (error: unknown) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2002') {
        throw new ConflictException('Email is already in use');
      }
      throw error;
    }
  }

  /** Update administrator-controlled fields and invalidate sessions when authorization changes. */
  public async updateUser(
    userId: string,
    input: UpdateUserDto,
    context: AuditContext,
  ): Promise<UserResource> {
    if (input.displayName === undefined && input.role === undefined && input.status === undefined) {
      throw new ConflictException('No update requested');
    }

    try {
      const updated = await this.database.client.$transaction(async (transaction) => {
        const current = await transaction.user.findUnique({ where: { id: userId } });
        if (!current) {
          throw new NotFoundException('User not found');
        }
        // Role or status changes alter authorization, so prior token security version must expire.
        const securityChanged =
          (input.role !== undefined && input.role !== current.role) ||
          (input.status !== undefined && input.status !== current.status);
        const user = await transaction.user.update({
          where: { id: userId },
          data: {
            ...(input.displayName === undefined ? {} : { displayName: input.displayName.trim() }),
            ...(input.role === undefined ? {} : { role: input.role }),
            ...(input.status === undefined ? {} : { status: input.status }),
            version: { increment: 1 },
            ...(securityChanged ? { securityVersion: { increment: 1 } } : {}),
          },
        });
        await transaction.auditLog.create({
          data: {
            actorId: context.actorId,
            action: 'user.admin.updated',
            targetId: userId,
            ...(context.requestId === undefined ? {} : { requestId: context.requestId }),
          },
        });
        return user;
      });
      return this.toResource(updated);
    } catch (error: unknown) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2025') {
        throw new NotFoundException('User not found');
      }
      throw error;
    }
  }

  /** Report whether bootstrap-safe user database is already populated. */
  public async hasUsers(): Promise<boolean> {
    return (await this.database.client.user.count()) > 0;
  }

  /** Project database record into minimal mutable authorization snapshot. */
  private toAuthenticatedUser(user: {
    id: string;
    email: string;
    displayName: string;
    role: Role;
    status: UserStatus;
    securityVersion: number;
  }): AuthenticatedUser {
    return {
      id: user.id,
      email: user.email,
      displayName: user.displayName,
      role: user.role,
      status: user.status,
      securityVersion: user.securityVersion,
    };
  }

  /** Project database record into public API resource with serialized timestamps. */
  private toResource(user: {
    id: string;
    email: string;
    displayName: string;
    role: Role;
    status: UserStatus;
    createdAt: Date;
    updatedAt: Date;
  }): UserResource {
    return {
      id: user.id,
      email: user.email,
      displayName: user.displayName,
      role: user.role,
      status: user.status,
      createdAt: user.createdAt.toISOString(),
      updatedAt: user.updatedAt.toISOString(),
    };
  }
}

/** Normalize email identifiers for case-insensitive uniqueness and lookup. */
export function normalizeEmail(email: string): string {
  return email.trim().toLowerCase();
}

/** Hash password using configured Argon2 parameters. */
async function hashPassword(password: string): Promise<string> {
  return argon2.hash(password, PASSWORD_OPTIONS);
}
