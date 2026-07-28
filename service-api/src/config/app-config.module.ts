import { Global, Module } from '@nestjs/common';

import { AppConfigService } from './app-config.service.js';

/** 全局提供经过环境校验的集中配置访问器。 */
@Global()
@Module({
  providers: [AppConfigService],
  exports: [AppConfigService],
})
export class AppConfigModule {}
