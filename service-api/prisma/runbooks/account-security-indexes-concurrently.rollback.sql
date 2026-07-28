-- 只在回滚本能力且确认查询性能可接受时逐条、事务外执行。
DROP INDEX CONCURRENTLY IF EXISTS "users_role_status_last_login_at_idx";
DROP INDEX CONCURRENTLY IF EXISTS "audit_logs_action_occurred_at_id_idx";
DROP INDEX CONCURRENTLY IF EXISTS "audit_logs_occurred_at_id_idx";
DROP INDEX CONCURRENTLY IF EXISTS "sessions_user_id_revoked_at_created_at_id_idx";
