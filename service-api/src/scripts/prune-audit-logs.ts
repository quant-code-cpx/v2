import { pathToFileURL } from 'node:url';

import { Client, type QueryResult, type QueryResultRow } from 'pg';

export const AUDIT_RETENTION_DAYS = 90;
export const AUDIT_RETENTION_BATCH_SIZE = 5_000;

/** 描述保留任务所需的最小 PostgreSQL 查询能力，便于无真实数据库单元测试。 */
export interface AuditRetentionClient {
  query<Row extends QueryResultRow = QueryResultRow>(
    text: string,
    values?: unknown[],
  ): Promise<QueryResult<Row>>;
}

/** 表示一次独立保留任务的可观测终态。 */
export type AuditRetentionResult = {
  status: 'completed' | 'skipped';
  deletedRows: number;
  batches: number;
  durationMs: number;
  cutoff: string;
};

type AdvisoryLockRow = {
  acquired: boolean;
};

/** 在全局 advisory lock 下分批删除九十天前审计，且不为清理动作再写审计。 */
export async function pruneAuditLogs(
  client: AuditRetentionClient,
  now: Date = new Date(),
): Promise<AuditRetentionResult> {
  const startedAt = Date.now();
  const cutoff = new Date(now.getTime() - AUDIT_RETENTION_DAYS * 24 * 60 * 60 * 1_000);
  const lock = await client.query<AdvisoryLockRow>(
    "SELECT pg_try_advisory_lock(hashtext('apex-audit-retention')) AS acquired",
  );
  if (lock.rows[0]?.acquired !== true) {
    return {
      status: 'skipped',
      deletedRows: 0,
      batches: 0,
      durationMs: Date.now() - startedAt,
      cutoff: cutoff.toISOString(),
    };
  }

  let deletedRows = 0;
  let batches = 0;
  try {
    for (;;) {
      const result = await client.query(
        `WITH expired AS (
           SELECT "id"
           FROM "audit_logs"
           WHERE "occurred_at" < $1
           ORDER BY "occurred_at", "id"
           LIMIT $2
           FOR UPDATE SKIP LOCKED
         )
         DELETE FROM "audit_logs" AS target
         USING expired
         WHERE target."id" = expired."id"`,
        [cutoff, AUDIT_RETENTION_BATCH_SIZE],
      );
      const count = result.rowCount ?? 0;
      deletedRows += count;
      batches += 1;
      if (count < AUDIT_RETENTION_BATCH_SIZE) {
        break;
      }
    }
  } finally {
    await client.query("SELECT pg_advisory_unlock(hashtext('apex-audit-retention'))");
  }

  return {
    status: 'completed',
    deletedRows,
    batches,
    durationMs: Date.now() - startedAt,
    cutoff: cutoff.toISOString(),
  };
}

/** 使用显式 DATABASE_URL 执行一次维护任务，并只输出非敏感统计。 */
async function main(): Promise<void> {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error('DATABASE_URL is required');
  }
  const client = new Client({ connectionString: databaseUrl });
  await client.connect();
  try {
    const result = await pruneAuditLogs(client);
    process.stdout.write(`${JSON.stringify({ operation: 'audit-retention', ...result })}\n`);
  } finally {
    await client.end();
  }
}

/** 识别直接 Node 执行，防止测试导入时连接数据库。 */
function isDirectExecution(): boolean {
  const entry = process.argv[1];
  return entry !== undefined && import.meta.url === pathToFileURL(entry).href;
}

// 失败仅输出维护任务错误原因和确定的非零退出码，不输出连接串。
if (isDirectExecution()) {
  void main().catch((error: unknown) => {
    process.stderr.write(
      `${JSON.stringify({
        operation: 'audit-retention',
        status: 'failed',
        reason: error instanceof Error ? error.message : 'Audit retention failed',
      })}\n`,
    );
    process.exitCode = 1;
  });
}
