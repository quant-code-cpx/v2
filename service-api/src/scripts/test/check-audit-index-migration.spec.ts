import type { QueryResult, QueryResultRow } from 'pg';
import { describe, expect, it, vi } from 'vitest';

import {
  AUDIT_INDEX_REGULAR_MIGRATION_LIMIT,
  checkAuditIndexMigration,
  type AuditIndexGateClient,
} from '../check-audit-index-migration.js';

// 汇集普通索引 migration 的百万行发布门禁测试。
describe('checkAuditIndexMigration', () => {
  // 验证新库尚无 audit_logs 时按零行放行初始 migration。
  it('allows fresh databases without an audit table', async () => {
    const query = vi.fn().mockResolvedValue(result([{ table_exists: false }]));

    await expect(checkAuditIndexMigration({ query } as AuditIndexGateClient)).resolves.toBe(0);
    expect(query).toHaveBeenCalledOnce();
  });

  // 验证少于百万行时允许普通 Prisma migration。
  it('allows regular migration below the threshold', async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce(result([{ table_exists: true }]))
      .mockResolvedValueOnce(result([{ count: '999999' }]));

    await expect(checkAuditIndexMigration({ query } as AuditIndexGateClient)).resolves.toBe(
      999_999,
    );
  });

  // 验证达到百万行立即阻断并指向受控并发索引 runbook。
  it('blocks regular migration at the threshold', async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce(result([{ table_exists: true }]))
      .mockResolvedValueOnce(result([{ count: String(AUDIT_INDEX_REGULAR_MIGRATION_LIMIT) }]));

    await expect(checkAuditIndexMigration({ query } as AuditIndexGateClient)).rejects.toThrow(
      'account-security-indexes-concurrently.sql',
    );
  });
});

/** 构造 pg 只读查询结果 fixture。 */
function result<Row extends QueryResultRow>(rows: Row[]): QueryResult<Row> {
  return {
    command: 'SELECT',
    rowCount: rows.length,
    oid: 0,
    rows,
    fields: [],
  };
}
