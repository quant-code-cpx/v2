-- 为数据运维 API 增加本地授权意图与可靠 HTTP 交付账本；同步事实仍由 data-sync 持有。
CREATE TYPE "DataOperationAction" AS ENUM (
  'SYNC_SUBMIT', 'SYNC_CANCEL', 'SYNC_RETRY', 'HEALTH_CHECK_SUBMIT',
  'SCHEDULE_UPSERT', 'SCHEDULE_SET_ENABLED'
);

CREATE TYPE "DataOperationDeliveryStatus" AS ENUM (
  'PENDING', 'DELIVERING', 'ACCEPTED', 'REJECTED', 'DEAD_LETTER'
);

CREATE TYPE "DataOperationResult" AS ENUM (
  'UNKNOWN', 'QUEUED', 'RUNNING', 'CANCEL_REQUESTED', 'SUCCEEDED', 'PARTIAL',
  'FAILED', 'CANCELLED', 'INTERRUPTED', 'SKIPPED', 'REJECTED'
);

CREATE TYPE "DataOperationAuthorityType" AS ENUM ('COMMAND', 'RUN', 'HEALTH_CHECK', 'SCHEDULE');
CREATE TYPE "ApiOutboxState" AS ENUM ('PENDING', 'DELIVERING', 'DELIVERED', 'DEAD_LETTER');

CREATE TABLE "data_operation_submissions" (
  "id" UUID NOT NULL,
  "actor_id" UUID,
  "actor_role" "Role" NOT NULL,
  "action" "DataOperationAction" NOT NULL,
  "idempotency_key" TEXT NOT NULL,
  "request_hash" CHAR(64) NOT NULL,
  "sanitized_request" JSONB NOT NULL,
  "actor_ref" TEXT NOT NULL,
  "reason" TEXT NOT NULL,
  "delivery_status" "DataOperationDeliveryStatus" NOT NULL DEFAULT 'PENDING',
  "operation_result" "DataOperationResult" NOT NULL DEFAULT 'UNKNOWN',
  "authority_type" "DataOperationAuthorityType",
  "authority_id" UUID,
  "queue_position" INTEGER,
  "request_id" TEXT NOT NULL,
  "safe_error" JSONB,
  "version" INTEGER NOT NULL DEFAULT 1,
  "authorized_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "completed_at" TIMESTAMPTZ,
  "last_observed_at" TIMESTAMPTZ,
  CONSTRAINT "data_operation_submissions_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "data_operation_submissions_actor_id_fkey"
    FOREIGN KEY ("actor_id") REFERENCES "users"("id") ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE TABLE "api_outboxes" (
  "id" UUID NOT NULL,
  "submission_id" UUID NOT NULL,
  "downstream_idempotency_key" TEXT NOT NULL,
  "internal_path" TEXT NOT NULL,
  "canonical_payload" JSONB NOT NULL,
  "state" "ApiOutboxState" NOT NULL DEFAULT 'PENDING',
  "attempt_count" INTEGER NOT NULL DEFAULT 0,
  "next_attempt_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "lease_owner" TEXT,
  "lease_until" TIMESTAMPTZ,
  "last_problem_code" TEXT,
  "last_attempt_at" TIMESTAMPTZ,
  "delivered_at" TIMESTAMPTZ,
  "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "api_outboxes_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "api_outboxes_submission_id_fkey"
    FOREIGN KEY ("submission_id") REFERENCES "data_operation_submissions"("id") ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE "data_operation_search_cursors" (
  "id" UUID NOT NULL,
  "actor_id" UUID NOT NULL,
  "fingerprint" TEXT NOT NULL,
  "occurred_to" TIMESTAMPTZ NOT NULL,
  "local_authorized_at" TIMESTAMPTZ,
  "local_id" UUID,
  "event_cursor" TEXT,
  "local_exhausted" BOOLEAN NOT NULL DEFAULT FALSE,
  "event_exhausted" BOOLEAN NOT NULL DEFAULT FALSE,
  "version" INTEGER NOT NULL DEFAULT 1,
  "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "data_operation_search_cursors_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "data_operation_submissions_actor_id_idempotency_key_key"
  ON "data_operation_submissions"("actor_id", "idempotency_key");
CREATE INDEX "data_operation_submissions_delivery_status_updated_at_id_idx"
  ON "data_operation_submissions"("delivery_status", "updated_at", "id");
CREATE INDEX "data_operation_submissions_authority_type_authority_id_idx"
  ON "data_operation_submissions"("authority_type", "authority_id");
CREATE INDEX "data_operation_submissions_actor_ref_idx"
  ON "data_operation_submissions"("actor_ref");
CREATE INDEX "data_operation_submissions_request_id_idx"
  ON "data_operation_submissions"("request_id");
CREATE UNIQUE INDEX "api_outboxes_submission_id_key" ON "api_outboxes"("submission_id");
CREATE UNIQUE INDEX "api_outboxes_downstream_idempotency_key_key"
  ON "api_outboxes"("downstream_idempotency_key");
CREATE INDEX "api_outboxes_state_next_attempt_at_id_idx"
  ON "api_outboxes"("state", "next_attempt_at", "id");
CREATE INDEX "api_outboxes_lease_until_idx" ON "api_outboxes"("lease_until");
CREATE INDEX "data_operation_search_cursors_actor_id_updated_at_idx"
  ON "data_operation_search_cursors"("actor_id", "updated_at");
