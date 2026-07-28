import { Module } from '@nestjs/common';

import { AuditController } from './audit.controller.js';
import { AuditService } from './audit.service.js';

/** 装配只读审计查询能力，不导出用户或会话写权限。 */
@Module({
  controllers: [AuditController],
  providers: [AuditService],
})
export class AuditModule {}
