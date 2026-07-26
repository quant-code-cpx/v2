-- Apex account authentication, hierarchical roles, soft deletion, and refresh-session families.
-- Existing user records require scripts/prepare-legacy-accounts.ts before this change. The
-- preparation step receives only explicit id-to-account mappings and never derives an account
-- from email. It creates nullable account columns transactionally; this migration validates and
-- contracts them. Empty databases continue through the normal fresh-install flow.

DO $$
DECLARE
  account_column_count INTEGER;
  duplicate_accounts BOOLEAN;
  invalid_accounts BOOLEAN;
  missing_accounts BOOLEAN;
BEGIN
  IF EXISTS (SELECT 1 FROM "users") THEN
    SELECT count(*) INTO account_column_count
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'users'
      AND column_name IN ('account', 'normalized_account');

    IF account_column_count <> 2 THEN
      RAISE EXCEPTION
        'Populated users require explicit account preparation: run prepare-legacy-accounts before applying 20260726010100_apex_user_access';
    END IF;

    EXECUTE 'SELECT EXISTS (SELECT 1 FROM "users" WHERE "account" IS NULL OR "normalized_account" IS NULL)'
      INTO missing_accounts;
    IF missing_accounts THEN
      RAISE EXCEPTION
        'Populated users require complete explicit account mappings before applying 20260726010100_apex_user_access';
    END IF;

    EXECUTE 'SELECT EXISTS (
      SELECT 1
      FROM "users"
      WHERE "account" !~ ''^[a-z0-9][a-z0-9._-]{4,31}$''
         OR "normalized_account" <> "account"
    )' INTO invalid_accounts;
    IF invalid_accounts THEN
      RAISE EXCEPTION
        'Explicit account mappings must contain normalized 5-32 character custom accounts';
    END IF;

    EXECUTE 'SELECT EXISTS (
      SELECT "normalized_account"
      FROM "users"
      GROUP BY "normalized_account"
      HAVING count(*) > 1
    )' INTO duplicate_accounts;
    IF duplicate_accounts THEN
      RAISE EXCEPTION
        'Explicit account mappings must not duplicate normalized accounts';
    END IF;
  END IF;
END
$$;

ALTER TABLE "users"
  ADD COLUMN IF NOT EXISTS "normalized_account" TEXT,
  ADD COLUMN IF NOT EXISTS "account" TEXT,
  ADD COLUMN "deleted_at" TIMESTAMP(3),
  ALTER COLUMN "normalized_email" DROP NOT NULL,
  ALTER COLUMN "email" DROP NOT NULL;

ALTER TABLE "users"
  ALTER COLUMN "normalized_account" SET NOT NULL,
  ALTER COLUMN "account" SET NOT NULL;

ALTER TABLE "users"
  ADD CONSTRAINT "users_account_format_check"
    CHECK ("account" ~ '^[a-z0-9][a-z0-9._-]{4,31}$' AND "normalized_account" = "account"),
  ADD CONSTRAINT "users_deleted_state_check"
    CHECK (("status" = 'DELETED') = ("deleted_at" IS NOT NULL));

CREATE UNIQUE INDEX "users_normalized_account_key" ON "users"("normalized_account");
CREATE UNIQUE INDEX "users_single_super_admin_key" ON "users" ((1)) WHERE "role" = 'SUPER_ADMIN';
CREATE INDEX "users_status_role_created_at_id_idx" ON "users"("status", "role", "created_at", "id");

ALTER TABLE "sessions"
  ADD COLUMN "family_id" UUID,
  ADD COLUMN "absolute_expires_at" TIMESTAMP(3),
  ADD COLUMN "rotated_at" TIMESTAMP(3);

UPDATE "sessions"
SET "family_id" = "id",
    "absolute_expires_at" = "expires_at"
WHERE "family_id" IS NULL OR "absolute_expires_at" IS NULL;

ALTER TABLE "sessions"
  ALTER COLUMN "family_id" SET NOT NULL,
  ALTER COLUMN "absolute_expires_at" SET NOT NULL;

CREATE INDEX "sessions_family_id_revoked_at_expires_at_idx"
  ON "sessions"("family_id", "revoked_at", "expires_at");
