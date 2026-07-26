import {
  Body,
  Controller,
  Delete,
  Get,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  ParseUUIDPipe,
  Patch,
  Post,
  Query,
  Req,
  Res,
} from '@nestjs/common';
import { ApiBearerAuth, ApiCreatedResponse, ApiNoContentResponse, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import { Role } from '../../generated/prisma/client.js';
import type { AuthenticatedRequest } from '../../platform/http/auth-context.js';
import { PublicProblemException } from '../../platform/http/problem.exception.js';
import { Roles } from '../../platform/http/roles.decorator.js';
import { AppConfigService } from '../../platform/config/app-config.service.js';
import { clearRefreshCookie } from '../../platform/http/refresh-cookie.js';
import { ChangePasswordDto } from './dto/change-password.dto.js';
import { CreateUserDto } from './dto/create-user.dto.js';
import { ListUsersQueryDto } from './dto/list-users-query.dto.js';
import { ResetPasswordDto } from './dto/reset-password.dto.js';
import { UpdateProfileDto } from './dto/update-profile.dto.js';
import { UpdateUserDto } from './dto/update-user.dto.js';
import { UserService } from './user.service.js';
import type { CurrentUserResource, UserPage, UserResource } from './user.types.js';

@ApiTags('users')
@ApiBearerAuth()
@Controller('users')
export class UserController {
  /** Wire globally authenticated HTTP requests to target-policy-aware UserService use cases. */
  public constructor(
    private readonly users: UserService,
    private readonly config: AppConfigService,
  ) {}

  @Get('me')
  /** Return current profile, effective permissions, and strong ETag for self profile updates. */
  public async getMe(
    @Req() request: AuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<CurrentUserResource> {
    const user = await this.users.getMe(request.user.userId);
    response.setHeader('ETag', userEtag(user.id, user.version));
    return user;
  }

  @Patch('me')
  /** Update current profile only when supplied ETag still identifies its loaded version. */
  public async updateMe(
    @Req() request: AuthenticatedRequest,
    @Headers('if-match') ifMatch: string | undefined,
    @Body() input: UpdateProfileDto,
    @Res({ passthrough: true }) response: Response,
  ): Promise<CurrentUserResource> {
    const user = await this.users.updateMe(
      request.user,
      input,
      parseIfMatch(ifMatch, request.user.userId),
      { actorId: request.user.userId, requestId: request.requestId },
    );
    response.setHeader('ETag', userEtag(user.id, user.version));
    return user;
  }

  @Post('me/password')
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** Change caller password, invalidate every prior session, and clear this browser refresh cookie. */
  public async changePassword(
    @Req() request: AuthenticatedRequest,
    @Body() input: ChangePasswordDto,
    @Res({ passthrough: true }) response: Response,
  ): Promise<void> {
    await this.users.changePassword(request.user, input, {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
    clearRefreshCookie(response, this.config);
    response.setHeader('Cache-Control', 'no-store');
  }

  @Get()
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** Return manageable targets plus the actor's own read-only row without exposing peer super admins. */
  public list(
    @Req() request: AuthenticatedRequest,
    @Query() query: ListUsersQueryDto,
  ): Promise<UserPage> {
    return this.users.listUsers(request.user, query);
  }

  @Post()
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  @ApiCreatedResponse()
  /** Create a target permitted by actor hierarchy and return its resource location and ETag. */
  public async create(
    @Req() request: AuthenticatedRequest,
    @Body() input: CreateUserDto,
    @Res({ passthrough: true }) response: Response,
  ): Promise<UserResource> {
    const user = await this.users.createUser(request.user, input, {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
    response.setHeader('ETag', userEtag(user.id, user.version));
    response.setHeader('Location', `/${this.config.apiPrefix}/users/${user.id}`);
    return user;
  }

  @Get(':id')
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** Return one target resource only when it falls within actor's backend-enforced scope. */
  public async getUser(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) userId: string,
    @Res({ passthrough: true }) response: Response,
  ): Promise<UserResource> {
    const user = await this.users.getUser(request.user, userId);
    response.setHeader('ETag', userEtag(user.id, user.version));
    return user;
  }

  @Patch(':id')
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** Update a managed target under ETag protection and return its next version. */
  public async update(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) userId: string,
    @Headers('if-match') ifMatch: string | undefined,
    @Body() input: UpdateUserDto,
    @Res({ passthrough: true }) response: Response,
  ): Promise<UserResource> {
    const user = await this.users.updateUser(
      request.user,
      userId,
      input,
      parseIfMatch(ifMatch, userId),
      { actorId: request.user.userId, requestId: request.requestId },
    );
    response.setHeader('ETag', userEtag(user.id, user.version));
    return user;
  }

  @Delete(':id')
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** Soft-delete a managed target under ETag protection while preserving a repeat-delete terminal result. */
  public async delete(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) userId: string,
    @Headers('if-match') ifMatch: string | undefined,
  ): Promise<void> {
    await this.users.deleteUser(request.user, userId, parseIfMatch(ifMatch, userId), {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
  }

  @Post(':id/password-reset')
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** Reset a managed target password without echoing it and invalidate all target sessions. */
  public async resetPassword(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) userId: string,
    @Headers('if-match') ifMatch: string | undefined,
    @Body() input: ResetPasswordDto,
  ): Promise<void> {
    await this.users.resetPassword(request.user, userId, input, parseIfMatch(ifMatch, userId), {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
  }
}

/** Render a stable strong ETag binding user identity and optimistic-concurrency version. */
function userEtag(userId: string, version: number): string {
  return `"user-${userId}-v${version}"`;
}

/** Require an ETag for mutation and reject malformed or cross-resource validators safely. */
function parseIfMatch(ifMatch: string | undefined, userId: string): number {
  if (ifMatch === undefined) {
    throw new PublicProblemException(
      HttpStatus.PRECONDITION_REQUIRED,
      'precondition-required',
      'If-Match is required',
    );
  }
  const match = /^"user-([0-9a-f-]{36})-v([1-9][0-9]*)"$/i.exec(ifMatch);
  if (!match || match[1] !== userId) {
    throw new PublicProblemException(
      HttpStatus.PRECONDITION_FAILED,
      'precondition-failed',
      'User changed since it was loaded',
    );
  }
  const version = Number(match[2]);
  if (!Number.isSafeInteger(version)) {
    throw new PublicProblemException(
      HttpStatus.PRECONDITION_FAILED,
      'precondition-failed',
      'User changed since it was loaded',
    );
  }
  return version;
}
