-- Manual rollback for 20260726000000_initial_user_auth.
-- Run only before any later migration depends on these tables or enum values.

DROP TABLE IF EXISTS "audit_logs";
DROP TABLE IF EXISTS "sessions";
DROP TABLE IF EXISTS "credentials";
DROP TABLE IF EXISTS "users";
DROP TYPE IF EXISTS "UserStatus";
DROP TYPE IF EXISTS "Role";
