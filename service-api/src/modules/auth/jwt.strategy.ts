import { Injectable } from '@nestjs/common';
import { PassportStrategy } from '@nestjs/passport';
import { ExtractJwt, Strategy } from 'passport-jwt';

import { AppConfigService } from '../../platform/config/app-config.service.js';
import { AuthService } from './auth.service.js';
import type { AuthContext, JwtPayload } from './auth.types.js';

@Injectable()
export class JwtStrategy extends PassportStrategy(Strategy) {
  /** Configure bearer-token extraction and delegate stateful claim validation. */
  public constructor(
    config: AppConfigService,
    private readonly auth: AuthService,
  ) {
    super({
      jwtFromRequest: ExtractJwt.fromAuthHeaderAsBearerToken(),
      ignoreExpiration: false,
      secretOrKey: config.jwtAccessSecret,
      issuer: config.jwtIssuer,
      audience: config.jwtAudience,
      algorithms: ['HS256'],
    });
  }

  /** Convert verified JWT payload into currently authorized request context. */
  public validate(payload: JwtPayload): Promise<AuthContext> {
    return this.auth.validateAccessToken(payload);
  }
}
