import {
  Body,
  Controller,
  Headers,
  HttpCode,
  HttpStatus,
  Param,
  ParseUUIDPipe,
  Post,
  Query,
  Req,
  Res,
} from '@nestjs/common';
import { ApiBearerAuth, ApiCreatedResponse, ApiNoContentResponse, ApiTags } from '@nestjs/swagger';

import type { Response } from 'express';

import { Role } from '../../generated/prisma/client.js';
import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import { PublicProblemException } from '../../common/exceptions/problem.exception.js';
import { Roles } from '../../common/decorators/roles.decorator.js';
import { AppConfigService } from '../../config/app-config.service.js';
import { clearRefreshCookie } from '../../common/utils/refresh-cookie.js';
import { ChangePasswordDto } from './dto/change-password.dto.js';
import { CreateUserDto } from './dto/create-user.dto.js';
import { ListUsersQueryDto } from './dto/list-users-query.dto.js';
import { ResetPasswordDto } from './dto/reset-password.dto.js';
import { UpdateProfileDto } from './dto/update-profile.dto.js';
import { UpdateUserDto } from './dto/update-user.dto.js';
import { UserService } from './user.service.js';
import type {
  CurrentUserResource,
  ManageableUserStatistics,
  UserPage,
  UserResource,
} from './user.types.js';

@ApiTags('users')
@ApiBearerAuth()
@Controller('users')
export class UserController {
  /** 将全局认证后的 HTTP 请求转交给执行目标级权限策略的 `UserService`。 */
  public constructor(
    private readonly users: UserService,
    private readonly config: AppConfigService,
  ) {}

  @Post('me')
  @HttpCode(HttpStatus.OK)
  /** 返回当前用户资料、有效权限及用于资料更新的强 `ETag`。 */
  public async getMe(
    @Req() request: AuthenticatedRequest,
    @Res({ passthrough: true }) response: Response,
  ): Promise<CurrentUserResource> {
    const user = await this.users.getMe(request.user.userId);
    response.setHeader('ETag', userEtag(user.id, user.version));
    return user;
  }

  @Post('me/update')
  @HttpCode(HttpStatus.OK)
  /** 仅在提交的 `ETag` 仍指向已加载版本时更新当前用户资料。 */
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
  /** 修改调用方密码、失效全部旧会话，并清除当前浏览器的 refresh cookie。 */
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

  @Post('list')
  @HttpCode(HttpStatus.OK)
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** 返回可管理目标和调用方自身只读记录，不暴露同级超级管理员。 */
  public list(
    @Req() request: AuthenticatedRequest,
    @Query() query: ListUsersQueryDto,
  ): Promise<UserPage> {
    return this.users.listUsers(request.user, query);
  }

  @Post('statistics')
  @HttpCode(HttpStatus.OK)
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** 返回调用方可管理角色范围内的用户聚合统计。 */
  public statistics(@Req() request: AuthenticatedRequest): Promise<ManageableUserStatistics> {
    return this.users.getManageableStatistics(request.user);
  }

  @Post()
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  @ApiCreatedResponse()
  /** 创建调用方层级允许的目标，并返回资源位置与 `ETag`。 */
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

  @Post(':id')
  @HttpCode(HttpStatus.OK)
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** 仅当目标属于后端强制的调用方权限范围时返回该资源。 */
  public async getUser(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) userId: string,
    @Res({ passthrough: true }) response: Response,
  ): Promise<UserResource> {
    const user = await this.users.getUser(request.user, userId);
    response.setHeader('ETag', userEtag(user.id, user.version));
    return user;
  }

  @Post(':id/update')
  @HttpCode(HttpStatus.OK)
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  /** 在 `ETag` 保护下更新可管理目标，并返回其下一版本。 */
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

  @Post(':id/delete')
  @Roles(Role.ADMIN, Role.SUPER_ADMIN)
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** 在 `ETag` 保护下软删除可管理目标，并保持重复删除的终态结果。 */
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
  /** 重置可管理目标的密码且不回显，并失效目标的全部会话。 */
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

/** 生成绑定用户身份和乐观并发版本的稳定强 `ETag`。 */
function userEtag(userId: string, version: number): string {
  return `"user-${userId}-v${version}"`;
}

/** 要求变更请求携带 `ETag`，并安全拒绝畸形或跨资源校验值。 */
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
