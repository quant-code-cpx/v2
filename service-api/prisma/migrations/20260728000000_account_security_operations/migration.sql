-- 为本人会话、审计读取和可管理用户统计补充只读查询索引。
-- 若审计表已达百万行且索引尚未由并发任务创建，则普通 migration 必须停止。
DO $$
DECLARE
  audit_row_count BIGINT;
BEGIN
  SELECT count(*) INTO audit_row_count FROM "audit_logs";
  IF audit_row_count >= 1000000
     AND (
       to_regclass('public.audit_logs_occurred_at_id_idx') IS NULL
       OR to_regclass('public.audit_logs_action_occurred_at_id_idx') IS NULL
     ) THEN
    RAISE EXCEPTION
      'audit_logs has % rows; create account-security indexes with the reviewed concurrent runbook before retrying migration',
      audit_row_count;
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS "sessions_user_id_revoked_at_created_at_id_idx"
  ON "sessions"("user_id", "revoked_at", "created_at", "id");

CREATE INDEX IF NOT EXISTS "audit_logs_occurred_at_id_idx"
  ON "audit_logs"("occurred_at", "id");

CREATE INDEX IF NOT EXISTS "audit_logs_action_occurred_at_id_idx"
  ON "audit_logs"("action", "occurred_at", "id");

CREATE INDEX IF NOT EXISTS "users_role_status_last_login_at_idx"
  ON "users"("role", "status", "last_login_at");
