-- 仅在 audit_logs 达到 100 万行且普通 migration 被门禁阻断时执行。
-- 每条语句必须在事务外单独运行；完成后再次执行 Prisma migration 登记版本。
CREATE INDEX CONCURRENTLY IF NOT EXISTS "sessions_user_id_revoked_at_created_at_id_idx"
  ON "sessions"("user_id", "revoked_at", "created_at", "id");

CREATE INDEX CONCURRENTLY IF NOT EXISTS "audit_logs_occurred_at_id_idx"
  ON "audit_logs"("occurred_at", "id");

CREATE INDEX CONCURRENTLY IF NOT EXISTS "audit_logs_action_occurred_at_id_idx"
  ON "audit_logs"("action", "occurred_at", "id");

CREATE INDEX CONCURRENTLY IF NOT EXISTS "users_role_status_last_login_at_idx"
  ON "users"("role", "status", "last_login_at");
