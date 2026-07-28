import 'reflect-metadata';

import { plainToInstance } from 'class-transformer';
import { validate } from 'class-validator';
import { describe, expect, it } from 'vitest';

import { SessionListDto } from '../dto/session-list.dto.js';

// 汇集本人会话列表请求体的契约边界测试。
describe('SessionListDto', () => {
  // 验证合法游标和页长可以通过。
  it('accepts valid pagination fields', async () => {
    const input = plainToInstance(SessionListDto, {
      cursor: 'opaque-cursor',
      pageSize: 50,
    });

    await expect(validate(input)).resolves.toHaveLength(0);
  });

  // 验证 JSON 请求体不会把字符串数字静默转换成整数。
  it('rejects string page sizes', async () => {
    const input = plainToInstance(SessionListDto, {
      pageSize: '20',
    });

    const errors = await validate(input);
    expect(errors.map((error) => error.property)).toEqual(['pageSize']);
  });
});
