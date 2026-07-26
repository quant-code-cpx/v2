import {
  Body,
  Controller,
  Get,
  HttpCode,
  HttpStatus,
  Param,
  ParseUUIDPipe,
  Patch,
  Post,
  Query,
  Req,
  UseGuards,
} from '@nestjs/common';
import { ApiBearerAuth, ApiCreatedResponse, ApiNoContentResponse, ApiTags } from '@nestjs/swagger';

import { JwtAuthGuard } from '../auth/jwt-auth.guard.js';
import { Roles } from '../auth/roles.decorator.js';
import { RolesGuard } from '../auth/roles.guard.js';
import type { AuthenticatedRequest } from '../auth/auth.types.js';
import { ChangePasswordDto } from './dto/change-password.dto.js';
import { CreateUserDto } from './dto/create-user.dto.js';
import { ListUsersQueryDto } from './dto/list-users-query.dto.js';
import { UpdateProfileDto } from './dto/update-profile.dto.js';
import { UpdateUserDto } from './dto/update-user.dto.js';
import { UserService } from './user.service.js';
import type { UserPage, UserResource } from './user.types.js';

@ApiTags('users')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard, RolesGuard)
@Controller('users')
export class UserController {
  /** Wire guarded user routes to application service. */
  public constructor(private readonly users: UserService) {}

  @Get('me')
  /** Return authenticated user's current public profile. */
  public getMe(@Req() request: AuthenticatedRequest): Promise<UserResource> {
    return this.users.getMe(request.user.userId);
  }

  @Patch('me')
  /** Update authenticated user's own mutable profile fields and audit request. */
  public updateMe(
    @Req() request: AuthenticatedRequest,
    @Body() input: UpdateProfileDto,
  ): Promise<UserResource> {
    return this.users.updateMe(request.user.userId, input, {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
  }

  @Post('me/password')
  @HttpCode(HttpStatus.NO_CONTENT)
  @ApiNoContentResponse()
  /** Change caller password, invalidating prior authenticated state. */
  public async changePassword(
    @Req() request: AuthenticatedRequest,
    @Body() input: ChangePasswordDto,
  ): Promise<void> {
    await this.users.changePassword(request.user.userId, input, {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
  }

  @Get()
  @Roles('ADMIN')
  /** Return administrator-visible cursor page of users. */
  public list(@Query() query: ListUsersQueryDto): Promise<UserPage> {
    return this.users.listUsers(query);
  }

  @Post()
  @Roles('ADMIN')
  @ApiCreatedResponse()
  /** Create a user under administrator authority with auditable actor context. */
  public create(
    @Req() request: AuthenticatedRequest,
    @Body() input: CreateUserDto,
  ): Promise<UserResource> {
    return this.users.createUser(input, {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
  }

  @Patch(':id')
  @Roles('ADMIN')
  /** Update target user's profile, role, or status under administrator authority. */
  public update(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) userId: string,
    @Body() input: UpdateUserDto,
  ): Promise<UserResource> {
    return this.users.updateUser(userId, input, {
      actorId: request.user.userId,
      requestId: request.requestId,
    });
  }
}
