import { ExecutionContext, Injectable } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { AuthGuard } from '@nestjs/passport';

import { IS_PUBLIC_ROUTE } from '../../platform/http/public.decorator.js';

@Injectable()
export class DefaultJwtAuthGuard extends AuthGuard('jwt') {
  /** Receive route metadata used to make anonymous access an explicit allowlist exception. */
  public constructor(private readonly reflector: Reflector) {
    super();
  }

  /** Require bearer authentication for every HTTP handler except a deliberate @Public route. */
  public async canActivate(context: ExecutionContext): Promise<boolean> {
    const isPublic = this.reflector.getAllAndOverride<boolean>(IS_PUBLIC_ROUTE, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (isPublic) {
      return true;
    }
    return (await super.canActivate(context)) as boolean;
  }
}
