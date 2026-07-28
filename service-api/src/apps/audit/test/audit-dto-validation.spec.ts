import 'reflect-metadata';

import { plainToInstance } from 'class-transformer';
import { validate } from 'class-validator';
import { describe, expect, it } from 'vitest';

import { ListAuditEventsDto } from '../dto/list-audit-events.dto.js';

// 汇集审计筛选输入的边界验证测试。
describe('ListAuditEventsDto', () => {
  // 验证冻结分类、UUID、页长和 RFC 3339 时间可以通过。
  it('accepts valid bounded filters', async () => {
    const input = plainToInstance(ListAuditEventsDto, {
      category: 'AUTHENTICATION',
      actorId: '00000000-0000-4000-8000-000000000001',
      targetId: '00000000-0000-4000-8000-000000000002',
      occurredFrom: '2026-07-27T00:00:00.000Z',
      occurredTo: '2026-07-28T00:00:00.000Z',
      includeRoutine: true,
      pageSize: 100,
    });

    await expect(validate(input)).resolves.toHaveLength(0);
  });

  // 验证未知分类、畸形 UUID 和超限页长同时被拒绝。
  it('rejects invalid filters', async () => {
    const input = plainToInstance(ListAuditEventsDto, {
      category: 'SECRET',
      actorId: 'not-a-uuid',
      pageSize: 101,
    });

    const errors = await validate(input);
    // 仅投影字段名，避免断言依赖 class-validator 的本地化消息。
    expect(errors.map((error) => error.property).sort()).toEqual([
      'actorId',
      'category',
      'pageSize',
    ]);
  });

  // 验证 JSON 请求体不会把字符串布尔值或数字静默转换成契约类型。
  it('rejects string values for typed body fields', async () => {
    const input = plainToInstance(ListAuditEventsDto, {
      includeRoutine: 'false',
      pageSize: '20',
    });

    const errors = await validate(input);
    expect(errors.map((error) => error.property).sort()).toEqual(['includeRoutine', 'pageSize']);
  });
});
