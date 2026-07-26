import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

import { Client } from 'pg';

const ACCOUNT_PATTERN = /^[a-z0-9][a-z0-9._-]{4,31}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_MAPPING_FILE_BYTES = 1024 * 1024;

type LegacyAccountMapping = {
  userId: string;
  account: string;
};

type LegacyUserRow = {
  id: string;
  account: string | null;
  normalized_account: string | null;
};

type ColumnRow = {
  column_name: string;
  is_nullable: 'YES' | 'NO';
};

/** Read one controlled mapping file while bounding input size before JSON parsing. */
async function readLegacyAccountMappings(filePath: string): Promise<LegacyAccountMapping[]> {
  const contents = await readFile(filePath, 'utf8');
  if (Buffer.byteLength(contents, 'utf8') > MAX_MAPPING_FILE_BYTES) {
    throw new Error('LEGACY_ACCOUNT_MAPPING_FILE exceeds 1 MiB');
  }
  return parseLegacyAccountMappings(contents);
}

/** Parse explicit id-to-account mappings without accepting emails, inferred identities, or extra fields. */
export function parseLegacyAccountMappings(contents: string): LegacyAccountMapping[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(contents);
  } catch {
    throw new Error('LEGACY_ACCOUNT_MAPPING_FILE must contain valid JSON');
  }
  if (!Array.isArray(parsed)) {
    throw new Error('LEGACY_ACCOUNT_MAPPING_FILE must be a JSON array');
  }

  const ids = new Set<string>();
  const accounts = new Set<string>();
  return parsed.map((value, index) => {
    if (!isRecord(value) || !hasExactMappingFields(value)) {
      throw new Error(`Mapping entry ${index} must contain only userId and account`);
    }
    const userId = value.userId;
    const account = value.account;
    if (typeof userId !== 'string' || !UUID_PATTERN.test(userId)) {
      throw new Error(`Mapping entry ${index} has an invalid userId`);
    }
    if (typeof account !== 'string') {
      throw new Error(`Mapping entry ${index} has an invalid account`);
    }
    const normalizedAccount = account.trim().toLowerCase();
    if (!ACCOUNT_PATTERN.test(normalizedAccount)) {
      throw new Error(`Mapping entry ${index} has an invalid account`);
    }
    const normalizedId = userId.toLowerCase();
    if (ids.has(normalizedId) || accounts.has(normalizedAccount)) {
      throw new Error(`Mapping entry ${index} duplicates a userId or account`);
    }
    ids.add(normalizedId);
    accounts.add(normalizedAccount);
    return { userId: normalizedId, account: normalizedAccount };
  });
}

/** Require a supplied mapping to cover every locked legacy user once and never target unknown users. */
export function assertMappingCoverage(
  mappings: readonly LegacyAccountMapping[],
  databaseUserIds: readonly string[],
): void {
  if (databaseUserIds.length === 0) {
    throw new Error('Refusing legacy account preparation: database contains no users');
  }
  const databaseIds = new Set(databaseUserIds.map((id) => id.toLowerCase()));
  const mappingIds = new Set(mappings.map((mapping) => mapping.userId));
  const mappingAccounts = new Set(mappings.map((mapping) => mapping.account));
  if (mappingIds.size !== mappings.length || mappingAccounts.size !== mappings.length) {
    throw new Error('Mapping must not duplicate userIds or accounts');
  }
  if (mappings.length !== databaseIds.size) {
    throw new Error('Mapping must contain exactly one account for every existing user');
  }
  if (mappings.some((mapping) => !databaseIds.has(mapping.userId))) {
    throw new Error('Mapping contains a userId absent from the database');
  }
}

/** Expand and fill explicit account columns under one maintenance-window transaction before Prisma migration 10100. */
export async function prepareLegacyAccounts(
  databaseUrl: string,
  mappings: readonly LegacyAccountMapping[],
): Promise<number> {
  const client = new Client({ connectionString: databaseUrl });
  await client.connect();
  try {
    await client.query('BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE');
    await client.query('SET LOCAL search_path TO public');
    await client.query("SELECT pg_advisory_xact_lock(hashtext('apex-legacy-account-mapping'))");
    await client.query('LOCK TABLE "users" IN ACCESS EXCLUSIVE MODE');
    const columns = await client.query<ColumnRow>(
      "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'users' AND column_name IN ('account', 'normalized_account')",
    );
    assertLegacyAccountColumns(columns.rows);
    if (columns.rows.length === 0) {
      await client.query(
        'ALTER TABLE "users" ADD COLUMN "normalized_account" TEXT, ADD COLUMN "account" TEXT',
      );
    }

    const users = await client.query<LegacyUserRow>(
      'SELECT "id"::text AS id, "account", "normalized_account" FROM "users" FOR UPDATE',
    );
    assertMappingCoverage(
      mappings,
      users.rows.map((user) => user.id),
    );
    const byUserId = new Map(mappings.map((mapping) => [mapping.userId, mapping.account]));
    const isPrepared = users.rows.every(
      (user) => user.account !== null && user.normalized_account !== null,
    );
    if (
      !isPrepared &&
      users.rows.some((user) => user.account !== null || user.normalized_account !== null)
    ) {
      throw new Error(
        'Legacy account columns are partially populated; resolve manually before retrying',
      );
    }

    for (const user of users.rows) {
      const account = byUserId.get(user.id.toLowerCase());
      if (!account) {
        throw new Error('Mapping coverage changed during preparation');
      }
      if (isPrepared) {
        if (user.account !== account || user.normalized_account !== account) {
          throw new Error('Prepared account values differ from supplied explicit mapping');
        }
        continue;
      }
      const update = await client.query(
        'UPDATE "users" SET "account" = $1, "normalized_account" = $1 WHERE "id" = $2::uuid AND "account" IS NULL AND "normalized_account" IS NULL',
        [account, user.id],
      );
      if (update.rowCount !== 1) {
        throw new Error('Legacy account preparation lost a concurrent update');
      }
    }
    await client.query('COMMIT');
    return users.rows.length;
  } catch (error: unknown) {
    await client.query('ROLLBACK').catch(() => undefined);
    throw error;
  } finally {
    await client.end();
  }
}

/** Reject already-migrated, half-created, or unexpected account columns before any legacy write occurs. */
function assertLegacyAccountColumns(columns: readonly ColumnRow[]): void {
  if (columns.length === 0) {
    return;
  }
  if (columns.length !== 2) {
    throw new Error('Legacy users table has an incomplete account-column state');
  }
  const names = new Set(columns.map((column) => column.column_name));
  if (!names.has('account') || !names.has('normalized_account')) {
    throw new Error('Legacy users table has an unexpected account-column state');
  }
  if (columns.some((column) => column.is_nullable === 'NO')) {
    throw new Error(
      'Apex account migration is already complete; legacy preparation is not allowed',
    );
  }
}

/** Narrow JSON values to own string-keyed records before checking mapping fields. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Require a minimal mapping shape so email and unrelated fields cannot become implicit inputs. */
function hasExactMappingFields(value: Record<string, unknown>): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === 2 && keys[0] === 'account' && keys[1] === 'userId';
}

/** Execute CLI preparation using only explicit DATABASE_URL and mapping-file environment inputs. */
async function main(): Promise<void> {
  const databaseUrl = process.env.DATABASE_URL;
  const mappingFile = process.env.LEGACY_ACCOUNT_MAPPING_FILE;
  if (!databaseUrl || !mappingFile) {
    throw new Error('DATABASE_URL and LEGACY_ACCOUNT_MAPPING_FILE are required');
  }
  const mappings = await readLegacyAccountMappings(mappingFile);
  const count = await prepareLegacyAccounts(databaseUrl, mappings);
  // Log only the count: account values and file content are not operational log fields.
  process.stdout.write(`Prepared explicit accounts for ${count} legacy users.\n`);
}

/** Detect direct Node execution so unit-test imports cannot start database work. */
function isDirectExecution(): boolean {
  const entry = process.argv[1];
  return entry !== undefined && import.meta.url === pathToFileURL(entry).href;
}

// Return a deterministic nonzero process status without echoing file contents or account values.
if (isDirectExecution()) {
  void main().catch((error: unknown) => {
    process.stderr.write(
      `${error instanceof Error ? error.message : 'Legacy account preparation failed'}\n`,
    );
    process.exitCode = 1;
  });
}
