import { CanActivate, ExecutionContext, Injectable } from '@nestjs/common';
import { Reflector } from '@nestjs/core';

import { ROLES_KEY } from '../../common/decorators/roles.decorator.js';
import type { AuthenticatedRequest } from '../../common/models/auth-context.js';

@Injectable()
export class RolesGuard implements CanActivate {
  /** Receive route metadata reader for coarse pre-use-case authorization. */
  public constructor(private readonly reflector: Reflector) {}

  /** Permit routes without role metadata, otherwise require an authenticated allowed role. */
  public canActivate(context: ExecutionContext): boolean {
    const requiredRoles = this.reflector.getAllAndOverride<string[]>(ROLES_KEY, [
      context.getHandler(),
      context.getClass(),
    ]);
    if (!requiredRoles?.length) {
      return true;
    }
    const request = context.switchToHttp().getRequest<AuthenticatedRequest>();
    return request.user !== undefined && requiredRoles.includes(request.user.role);
  }
}
