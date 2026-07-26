-- Manual rollback for 20260726010100_apex_user_access.
-- PostgreSQL enum values remain because removing enum values is destructive. Run only before
-- any Apex-only user row, soft deletion, or session-family field has been written.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM "users" WHERE "normalized_email" IS NULL OR "email" IS NULL)
     OR EXISTS (SELECT 1 FROM "users" WHERE "role" = 'SUPER_ADMIN' OR "status" = 'DELETED')
     OR EXISTS (SELECT 1 FROM "sessions" WHERE "family_id" <> "id" OR "rotated_at" IS NOT NULL) THEN
    RAISE EXCEPTION
      'Unsafe rollback: Apex account, role, deletion, or refresh-session state already exists';
  END IF;
END
$$;

DROP INDEX IF EXISTS "sessions_family_id_revoked_at_expires_at_idx";
ALTER TABLE "sessions"
  DROP COLUMN IF EXISTS "rotated_at",
  DROP COLUMN IF EXISTS "absolute_expires_at",
  DROP COLUMN IF EXISTS "family_id";

DROP INDEX IF EXISTS "users_status_role_created_at_id_idx";
DROP INDEX IF EXISTS "users_single_super_admin_key";
DROP INDEX IF EXISTS "users_normalized_account_key";
ALTER TABLE "users"
  DROP CONSTRAINT IF EXISTS "users_deleted_state_check",
  DROP CONSTRAINT IF EXISTS "users_account_format_check",
  DROP COLUMN IF EXISTS "deleted_at",
  DROP COLUMN IF EXISTS "account",
  DROP COLUMN IF EXISTS "normalized_account",
  ALTER COLUMN "normalized_email" SET NOT NULL,
  ALTER COLUMN "email" SET NOT NULL;
