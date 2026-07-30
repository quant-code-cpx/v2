/* eslint-disable @typescript-eslint/no-unsafe-function-type, @typescript-eslint/no-unsafe-return */

import { RequestMethod } from '@nestjs/common';
import { METHOD_METADATA, PATH_METADATA } from '@nestjs/common/constants.js';
import { describe, expect, it } from 'vitest';

import { Role } from '../../../generated/prisma/client.js';
import { ROLES_KEY } from '../../../common/decorators/roles.decorator.js';
import { DataOperationsController } from '../data-operations.controller.js';

/** 覆盖数据运维公开路由的 POST-only 与最小权限矩阵。 */
describe('DataOperationsController metadata', () => {
  /** 验证合同 0023 的十九条路由均声明为 POST，避免引入禁止的方法。 */
  it('declares all 19 public contract routes as POST', () => {
    const routes = controllerRoutes();

    expect(routes.map((route) => route.path)).toEqual([
      'overview',
      'datasets/search',
      'datasets/detail',
      'sync/preflight',
      'sync/submit',
      'sync/cancel',
      'sync/retry',
      'commands/detail',
      'runs/search',
      'runs/detail',
      'health/evaluations/search',
      'health/evaluations/detail',
      'health/checks/submit',
      'health/checks/detail',
      'schedules/search',
      'schedules/upsert',
      'schedules/set-enabled',
      'submissions/detail',
      'operations/search',
    ]);
    expect(routes.every((route) => route.method === RequestMethod.POST)).toBe(true);
  });

  /** 验证只有主动操作覆写为 SUPER_ADMIN，管理员仍保留全部只读视图。 */
  it('limits mutating operations to super administrators', () => {
    const routes = controllerRoutes();
    const writers = new Set([
      'sync/preflight',
      'sync/submit',
      'sync/cancel',
      'sync/retry',
      'health/checks/submit',
      'schedules/upsert',
      'schedules/set-enabled',
    ]);

    for (const route of routes) {
      const roles = Reflect.getMetadata(ROLES_KEY, route.handler) as Role[] | undefined;
      if (writers.has(route.path)) {
        expect(roles).toEqual([Role.SUPER_ADMIN]);
      } else {
        expect(roles).toBeUndefined();
      }
    }
    expect(Reflect.getMetadata(ROLES_KEY, DataOperationsController)).toEqual([
      Role.ADMIN,
      Role.SUPER_ADMIN,
    ]);
  });
});

/** 返回 Controller 原型上按定义顺序声明的 Nest 路由元数据。 */
function controllerRoutes(): Array<{ path: string; method: RequestMethod; handler: Function }> {
  return Object.getOwnPropertyNames(DataOperationsController.prototype)
    .map((name) => Object.getOwnPropertyDescriptor(DataOperationsController.prototype, name)?.value)
    .filter((handler): handler is Function => typeof handler === 'function')
    .flatMap((handler) => {
      const path = Reflect.getMetadata(PATH_METADATA, handler) as string | undefined;
      const method = Reflect.getMetadata(METHOD_METADATA, handler) as RequestMethod | undefined;
      return path === undefined || method === undefined ? [] : [{ path, method, handler }];
    });
}
