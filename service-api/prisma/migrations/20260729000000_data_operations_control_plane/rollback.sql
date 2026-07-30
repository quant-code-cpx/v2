-- 仅在从未写入数据运维意图时使用；已有 Submission 后只能向前修复，避免丢失审计证据。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM "data_operation_submissions" LIMIT 1) THEN
    RAISE EXCEPTION 'data operations rollback is unsafe after submissions exist';
  END IF;
END
$$;

DROP TABLE IF EXISTS "data_operation_search_cursors";
DROP TABLE IF EXISTS "api_outboxes";
DROP TABLE IF EXISTS "data_operation_submissions";
DROP TYPE IF EXISTS "ApiOutboxState";
DROP TYPE IF EXISTS "DataOperationAuthorityType";
DROP TYPE IF EXISTS "DataOperationResult";
DROP TYPE IF EXISTS "DataOperationDeliveryStatus";
DROP TYPE IF EXISTS "DataOperationAction";
