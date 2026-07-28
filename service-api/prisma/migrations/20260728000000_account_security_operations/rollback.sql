-- 此回滚只删除查询索引，不修改用户、会话或审计业务数据。
DROP INDEX IF EXISTS "users_role_status_last_login_at_idx";
DROP INDEX IF EXISTS "audit_logs_action_occurred_at_id_idx";
DROP INDEX IF EXISTS "audit_logs_occurred_at_id_idx";
DROP INDEX IF EXISTS "sessions_user_id_revoked_at_created_at_id_idx";
