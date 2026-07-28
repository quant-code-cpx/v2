import type { QueryResult, QueryResultRow } from 'pg';
import { describe, expect, it, vi } from 'vitest';

import {
  AUDIT_RETENTION_BATCH_SIZE,
  pruneAuditLogs,
  type AuditRetentionClient,
} from '../prune-audit-logs.js';

// 汇集审计保留任务的锁、批量边界和幂等终态测试。
describe('pruneAuditLogs', () => {
  // 验证获得 advisory lock 后按 5000 行分批删除并最终释放锁。
  it('deletes expired audit rows in bounded batches under one advisory lock', async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce(result([{ acquired: true }], 1))
      .mockResolvedValueOnce(result([], AUDIT_RETENTION_BATCH_SIZE))
      .mockResolvedValueOnce(result([], 2))
      .mockResolvedValueOnce(result([{ unlocked: true }], 1));
    const now = new Date('2026-07-28T00:00:00.000Z');

    const output = await pruneAuditLogs({ query } as AuditRetentionClient, now);

    expect(output).toEqual(
      expect.objectContaining({
        status: 'completed',
        deletedRows: 5_002,
        batches: 2,
        cutoff: '2026-04-29T00:00:00.000Z',
      }),
    );
    expect(query).toHaveBeenCalledTimes(4);
    expect(query.mock.calls[1]?.[1]).toEqual([
      new Date('2026-04-29T00:00:00.000Z'),
      AUDIT_RETENTION_BATCH_SIZE,
    ]);
    expect(String(query.mock.calls[3]?.[0])).toContain('pg_advisory_unlock');
  });

  // 验证未取得 advisory lock 时安全跳过，不执行删除或解锁其他实例的锁。
  it('skips when another retention instance owns the lock', async () => {
    const query = vi.fn().mockResolvedValue(result([{ acquired: false }], 1));

    await expect(pruneAuditLogs({ query } as AuditRetentionClient)).resolves.toEqual(
      expect.objectContaining({
        status: 'skipped',
        deletedRows: 0,
        batches: 0,
      }),
    );
    expect(query).toHaveBeenCalledOnce();
  });
});

/** 构造 pg 查询结果 fixture，只保留测试关心的 rows 与 rowCount。 */
function result<Row extends QueryResultRow>(rows: Row[], rowCount: number): QueryResult<Row> {
  return {
    command: 'TEST',
    rowCount,
    oid: 0,
    rows,
    fields: [],
  };
}
