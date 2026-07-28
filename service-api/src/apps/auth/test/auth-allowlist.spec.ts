import 'reflect-metadata';

import { readFile } from 'node:fs/promises';

import { HealthController } from '../../../shared/health/health.controller.js';
import { IS_PUBLIC_ROUTE } from '../../../common/decorators/public.decorator.js';
import { describe, expect, it } from 'vitest';

import { AuthController } from '../auth.controller.js';
import { UserController } from '../../user/user.controller.js';

// 汇集元数据断言，防止全局默认拒绝鉴权下的匿名路由意外扩张。
describe('default-deny anonymous allowlist', () => {
  // 验证只有浏览器登录流程与运维探针携带显式公开元数据。
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

  // 验证应用启动不能在 Controller 元数据白名单外增加匿名 Swagger 路由。
  it('does not register runtime Swagger routes outside the default-deny allowlist', async () => {
    const source = await readFile(new URL('../../../main.ts', import.meta.url), 'utf8');

    expect(source).not.toContain('SwaggerModule.setup');
    expect(source).not.toContain('openapi-json');
  });
});

/** 通过 descriptor 读取方法，使 lint 能确认该方法不会被意外地未绑定调用。 */
function handler(target: object, name: string): object {
  const value: unknown = Object.getOwnPropertyDescriptor(target, name)?.value;
  if (typeof value !== 'function') {
    throw new Error(`Missing method fixture: ${name}`);
  }
  return value;
}
