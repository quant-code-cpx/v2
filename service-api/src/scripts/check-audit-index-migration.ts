import { pathToFileURL } from 'node:url';

import { Client, type QueryResult, type QueryResultRow } from 'pg';

export const AUDIT_INDEX_REGULAR_MIGRATION_LIMIT = 1_000_000;

/** 描述索引门禁所需的最小 PostgreSQL 查询能力。 */
export interface AuditIndexGateClient {
  query<Row extends QueryResultRow = QueryResultRow>(text: string): Promise<QueryResult<Row>>;
}

type RelationRow = {
  table_exists: boolean;
};

type CountRow = {
  count: string;
};

/** 在普通 migration 前精确检查审计行数，达到百万行时强制改走并发 runbook。 */
export async function checkAuditIndexMigration(client: AuditIndexGateClient): Promise<number> {
  const relation = await client.query<RelationRow>(
    "SELECT to_regclass('public.audit_logs') IS NOT NULL AS table_exists",
  );
  if (relation.rows[0]?.table_exists !== true) {
    return 0;
  }
  const result = await client.query<CountRow>('SELECT count(*)::text AS count FROM "audit_logs"');
  const count = Number(result.rows[0]?.count ?? '0');
  if (!Number.isSafeInteger(count) || count < 0) {
    throw new Error('audit_logs count is invalid');
  }
  if (count >= AUDIT_INDEX_REGULAR_MIGRATION_LIMIT) {
    throw new Error(
      `audit_logs has ${count} rows; run prisma/runbooks/account-security-indexes-concurrently.sql before Prisma migration`,
    );
  }
  return count;
}

/** 使用显式 DATABASE_URL 执行发布前门禁并输出机器可读结果。 */
async function main(): Promise<void> {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error('DATABASE_URL is required');
  }
  const client = new Client({ connectionString: databaseUrl });
  await client.connect();
  try {
    const rowCount = await checkAuditIndexMigration(client);
    process.stdout.write(
      `${JSON.stringify({
        operation: 'audit-index-gate',
        status: 'regular-migration-allowed',
        rowCount,
        threshold: AUDIT_INDEX_REGULAR_MIGRATION_LIMIT,
      })}\n`,
    );
  } finally {
    await client.end();
  }
}

/** 识别直接 Node 执行，防止测试导入时连接数据库。 */
function isDirectExecution(): boolean {
  const entry = process.argv[1];
  return entry !== undefined && import.meta.url === pathToFileURL(entry).href;
}

// 门禁失败只输出必要原因；达到阈值时非零退出阻断普通发布。
if (isDirectExecution()) {
  void main().catch((error: unknown) => {
    process.stderr.write(
      `${JSON.stringify({
        operation: 'audit-index-gate',
        status: 'blocked',
        reason: error instanceof Error ? error.message : 'Audit index gate failed',
      })}\n`,
    );
    process.exitCode = 1;
  });
}
