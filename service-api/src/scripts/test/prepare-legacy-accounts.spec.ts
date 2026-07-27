import { describe, expect, it } from 'vitest';

import { assertMappingCoverage, parseLegacyAccountMappings } from '../prepare-legacy-accounts.js';

const firstUserId = '00000000-0000-4000-8000-000000000001';
const secondUserId = '00000000-0000-4000-8000-000000000002';

// 汇集映射文件检查，确保旧账号迁移显式且不依赖推断。
describe('legacy account mapping preparation', () => {
  // 验证账号值由调用方指定并规范化，不读取旧 email 字段。
  it('parses explicit id-to-account mappings and normalizes account casing', () => {
    expect(
      parseLegacyAccountMappings(
        JSON.stringify([{ userId: firstUserId, account: '  Market.Admin  ' }]),
      ),
    ).toEqual([{ userId: firstUserId, account: 'market.admin' }]);
  });

  // 验证 email 键不能混入输入形状，也不能用作迁移身份来源。
  it('rejects mapping entries containing email fields', () => {
    expect(() =>
      parseLegacyAccountMappings(
        JSON.stringify([
          { userId: firstUserId, account: 'market.admin', email: 'legacy@example.test' },
        ]),
      ),
    ).toThrow('only userId and account');
  });

  // 验证 DDL 收紧为非空前，每个持久化旧用户都恰好映射一次。
  it('requires complete and exact mapping coverage', () => {
    const mappings = parseLegacyAccountMappings(
      JSON.stringify([{ userId: firstUserId, account: 'market.admin' }]),
    );
    expect(() => assertMappingCoverage(mappings, [firstUserId, secondUserId])).toThrow(
      'exactly one account',
    );
    expect(() => assertMappingCoverage(mappings, [secondUserId])).toThrow(
      'absent from the database',
    );
  });
});
