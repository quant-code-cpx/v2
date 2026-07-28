import {
  Body,
  Controller,
  HttpCode,
  HttpStatus,
  Param,
  ParseUUIDPipe,
  Post,
  Req,
  Res,
} from '@nestjs/common';
import { ApiTags } from '@nestjs/swagger';
import { Role } from '../../generated/prisma/client.js';
import type { Response } from 'express';

import { Roles } from '../../common/decorators/roles.decorator.js';
import type { AuthenticatedRequest } from '../../common/models/auth-context.js';
import { AuditService } from './audit.service.js';
import type { AuditEventDetail, AuditEventPage } from './audit.types.js';
import { ListAuditEventsDto } from './dto/list-audit-events.dto.js';

@ApiTags('audit-events')
@Controller('audit-events')
@Roles(Role.SUPER_ADMIN)
export class AuditController {
  /** 注入审计读取用例，Controller 仅负责 HTTP 参数与缓存策略。 */
  public constructor(private readonly audit: AuditService) {}

  @Post('list')
  @HttpCode(HttpStatus.OK)
  /** 返回超级管理员可见的脱敏审计事件游标页。 */
  public async list(
    @Req() request: AuthenticatedRequest,
    @Body() input: ListAuditEventsDto,
    @Res({ passthrough: true }) response: Response,
  ): Promise<AuditEventPage> {
    const result = await this.audit.listEvents(request.user, input);
    response.setHeader('Cache-Control', 'no-store');
    return result;
  }

  @Post(':id')
  @HttpCode(HttpStatus.OK)
  /** 返回单个脱敏审计事件及 action 专属白名单详情。 */
  public async get(
    @Req() request: AuthenticatedRequest,
    @Param('id', new ParseUUIDPipe({ version: '4' })) eventId: string,
    @Res({ passthrough: true }) response: Response,
  ): Promise<AuditEventDetail> {
    const result = await this.audit.getEvent(request.user, eventId);
    response.setHeader('Cache-Control', 'no-store');
    return result;
  }
}
