import 'reflect-metadata';

import { readFile } from 'node:fs/promises';

import { HealthController } from '../../platform/health/health.controller.js';
import { IS_PUBLIC_ROUTE } from '../../platform/http/public.decorator.js';
import { describe, expect, it } from 'vitest';

import { AuthController } from './auth.controller.js';
import { UserController } from '../user/user.controller.js';

// Group metadata assertions preventing anonymous-route expansion under global default-deny authentication.
describe('default-deny anonymous allowlist', () => {
  // Verify only browser login workflow and operations probes carry explicit public metadata.
  it('marks exactly captcha, login, refresh, logout, health, and ready as public handlers', () => {
    const publicAuthHandlers = [
      handler(AuthController.prototype, 'createCaptcha'),
      handler(AuthController.prototype, 'login'),
      handler(AuthController.prototype, 'refresh'),
      handler(AuthController.prototype, 'logout'),
    ];

    expect(
      publicAuthHandlers.every((handler) => Reflect.getMetadata(IS_PUBLIC_ROUTE, handler) === true),
    ).toBe(true);
    expect(Reflect.getMetadata(IS_PUBLIC_ROUTE, HealthController)).toBe(true);
    expect(
      Reflect.getMetadata(IS_PUBLIC_ROUTE, handler(UserController.prototype, 'getMe')),
    ).toBeUndefined();
    expect(
      Reflect.getMetadata(IS_PUBLIC_ROUTE, handler(UserController.prototype, 'list')),
    ).toBeUndefined();
  });

  // Verify application bootstrap cannot add anonymous Swagger routes outside the controller metadata allowlist.
  it('does not register runtime Swagger routes outside the default-deny allowlist', async () => {
    const source = await readFile(new URL('../../main.ts', import.meta.url), 'utf8');

    expect(source).not.toContain('SwaggerModule.setup');
    expect(source).not.toContain('openapi-json');
  });
});

/** Read method function through descriptor so lint can prove it is not accidentally invoked unbound. */
function handler(target: object, name: string): object {
  const value: unknown = Object.getOwnPropertyDescriptor(target, name)?.value;
  if (typeof value !== 'function') {
    throw new Error(`Missing method fixture: ${name}`);
  }
  return value;
}
