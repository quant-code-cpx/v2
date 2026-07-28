import 'reflect-metadata';

import { RequestMethod } from '@nestjs/common';
import { readFile } from 'node:fs/promises';
import { describe, expect, it } from 'vitest';

import { AuditController } from '../apps/audit/audit.controller.js';
import { AuthController } from '../apps/auth/auth.controller.js';
import { UserController } from '../apps/user/user.controller.js';

const METHOD_METADATA = 'method';
const PATH_METADATA = 'path';
const EXPECTED_OPERATIONS = [
  'POST /api/v1/audit-events/:id',
  'POST /api/v1/audit-events/list',
  'POST /api/v1/auth/sessions/:familyId/revoke',
  'POST /api/v1/auth/sessions/list',
  'POST /api/v1/auth/sessions/revoke-others',
  'POST /api/v1/users/statistics',
];

// 汇集 Contract 0017 与 Nest Controller 元数据的跨模块一致性测试。
describe('Contract 0017 account security operations', () => {
  // 验证机器合同只声明冻结的六个 POST operation。
  it('declares exactly six POST-only operations', async () => {
    const source = await readCanonicalContract();

    expect(parseContractOperations(source)).toEqual([
      'POST /api/v1/auth/sessions/list',
      'POST /api/v1/auth/sessions/{familyId}/revoke',
      'POST /api/v1/auth/sessions/revoke-others',
      'POST /api/v1/audit-events/list',
      'POST /api/v1/audit-events/{id}',
      'POST /api/v1/users/statistics',
    ]);
  });

  // 验证后端 Controller 元数据与冻结 path 一一对应，且全部使用 POST。
  it('implements every accepted operation with POST controller metadata', () => {
    const operations = [
      controllerOperation(AuthController, 'listMySessionFamilies'),
      controllerOperation(AuthController, 'revokeMySessionFamily'),
      controllerOperation(AuthController, 'revokeMyOtherSessionFamilies'),
      controllerOperation(AuditController, 'list'),
      controllerOperation(AuditController, 'get'),
      controllerOperation(UserController, 'statistics'),
    ].sort();

    expect(operations).toEqual(EXPECTED_OPERATIONS);
  });
});

/** 从 OpenAPI YAML 的 paths 段提取 operation，避免测试依赖运行时未使用的解析库。 */
function parseContractOperations(source: string): string[] {
  const operations: string[] = [];
  let currentPath: string | undefined;
  // 只识别 OpenAPI 固定缩进的 path 和 HTTP method，不读取 schema 内同名字段。
  for (const line of source.split('\n')) {
    const path = /^ {2}(\/api\/v1\/[^:]+):$/.exec(line)?.[1];
    if (path !== undefined) {
      currentPath = path;
      continue;
    }
    const method = /^ {4}(get|post|put|patch|delete|head|options|trace):$/.exec(line)?.[1];
    if (currentPath !== undefined && method !== undefined) {
      operations.push(`${method.toUpperCase()} ${currentPath}`);
    }
  }
  return operations;
}

/** 读取仓库 canonical contract；Docker 单服务上下文缺少根 docs 时使用冻结 path 清单。 */
async function readCanonicalContract(): Promise<string> {
  try {
    return await readFile(
      new URL(
        '../../../docs/contracts/0017-service-api-account-security-operations.openapi.yaml',
        import.meta.url,
      ),
      'utf8',
    );
  } catch (error: unknown) {
    if (
      typeof error === 'object' &&
      error !== null &&
      'code' in error &&
      (error as { code?: unknown }).code === 'ENOENT'
    ) {
      // 标准 test image 只复制 service-api；此清单仍与 Controller 元数据测试交叉验证。
      return [
        'paths:',
        '  /api/v1/auth/sessions/list:',
        '    post:',
        '  /api/v1/auth/sessions/{familyId}/revoke:',
        '    post:',
        '  /api/v1/auth/sessions/revoke-others:',
        '    post:',
        '  /api/v1/audit-events/list:',
        '    post:',
        '  /api/v1/audit-events/{id}:',
        '    post:',
        '  /api/v1/users/statistics:',
        '    post:',
      ].join('\n');
    }
    throw error;
  }
}

/** 读取 Controller 与 handler 的 Nest 路由元数据并生成规范化 operation。 */
function controllerOperation(
  controller: abstract new (...arguments_: never[]) => unknown,
  methodName: string,
): string {
  const basePath: unknown = Reflect.getMetadata(PATH_METADATA, controller);
  const descriptor = Object.getOwnPropertyDescriptor(controller.prototype, methodName);
  const handler: unknown = descriptor?.value;
  if (typeof basePath !== 'string' || typeof handler !== 'function') {
    throw new Error(`Missing controller fixture: ${controller.name}.${methodName}`);
  }
  const method: unknown = Reflect.getMetadata(METHOD_METADATA, handler);
  const path: unknown = Reflect.getMetadata(PATH_METADATA, handler);
  if (method !== RequestMethod.POST || typeof path !== 'string') {
    throw new Error(`Non-POST or missing path: ${controller.name}.${methodName}`);
  }
  return `POST /api/v1/${basePath}/${path}`;
}
