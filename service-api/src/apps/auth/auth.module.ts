import { Module } from '@nestjs/common';
import { JwtModule } from '@nestjs/jwt';
import { APP_GUARD } from '@nestjs/core';
import { PassportModule } from '@nestjs/passport';

import { AppConfigService } from '../../config/app-config.service.js';
import { RolesGuard } from '../../lifecycle/guards/roles.guard.js';
import { UserModule } from '../user/user.module.js';
import { AuthController } from './auth.controller.js';
import { AuthService } from './auth.service.js';
import { BrowserRequestSecurityService } from './browser-request-security.service.js';
import {
  CAPTCHA_CODE_GENERATOR,
  CaptchaService,
  SecureCaptchaCodeGenerator,
} from './captcha.service.js';
import { DefaultJwtAuthGuard } from './default-jwt-auth.guard.js';
import { JwtStrategy } from './jwt.strategy.js';
import { SecurityRateLimitService } from './security-rate-limit.service.js';

@Module({
  imports: [
    UserModule,
    PassportModule.register({ defaultStrategy: 'jwt' }),
    JwtModule.registerAsync({
      inject: [AppConfigService],
      /** Derive JWT signing settings exclusively from validated application config. */
      useFactory: (config: AppConfigService) => ({
        secret: config.jwtAccessSecret,
        signOptions: {
          issuer: config.jwtIssuer,
          audience: config.jwtAudience,
          expiresIn: config.jwtAccessTtlSeconds,
        },
      }),
    }),
  ],
  controllers: [AuthController],
  providers: [
    AuthService,
    BrowserRequestSecurityService,
    CaptchaService,
    SecureCaptchaCodeGenerator,
    { provide: CAPTCHA_CODE_GENERATOR, useExisting: SecureCaptchaCodeGenerator },
    SecurityRateLimitService,
    JwtStrategy,
    DefaultJwtAuthGuard,
    RolesGuard,
    { provide: APP_GUARD, useExisting: DefaultJwtAuthGuard },
    { provide: APP_GUARD, useExisting: RolesGuard },
  ],
  exports: [AuthService, CaptchaService],
})
export class AuthModule {}
