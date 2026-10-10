CREATE TABLE IF NOT EXISTS hosted_artifact_budgets (
 environment TEXT PRIMARY KEY,
 manifest_sha256 TEXT NOT NULL,
 budget TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_budget_usage (
 environment TEXT PRIMARY KEY,
 live_bytes BIGINT NOT NULL DEFAULT 0 CHECK (live_bytes >= 0),
 lifetime_uploaded_bytes BIGINT NOT NULL DEFAULT 0 CHECK (lifetime_uploaded_bytes >= 0),
 lifetime_objects BIGINT NOT NULL DEFAULT 0 CHECK (lifetime_objects >= 0),
 put_attempts BIGINT NOT NULL DEFAULT 0 CHECK (put_attempts >= 0),
 get_attempts BIGINT NOT NULL DEFAULT 0 CHECK (get_attempts >= 0),
 list_attempts BIGINT NOT NULL DEFAULT 0 CHECK (list_attempts >= 0),
 delete_attempts BIGINT NOT NULL DEFAULT 0 CHECK (delete_attempts >= 0),
 delete_objects BIGINT NOT NULL DEFAULT 0 CHECK (delete_objects >= 0),
 egress_bytes BIGINT NOT NULL DEFAULT 0 CHECK (egress_bytes >= 0),
 control_response_bytes BIGINT NOT NULL DEFAULT 0 CHECK (control_response_bytes >= 0),
 cleanup_list_attempts BIGINT NOT NULL DEFAULT 0 CHECK (cleanup_list_attempts >= 0),
 cleanup_delete_attempts BIGINT NOT NULL DEFAULT 0 CHECK (cleanup_delete_attempts >= 0),
 cleanup_delete_objects BIGINT NOT NULL DEFAULT 0 CHECK (cleanup_delete_objects >= 0),
 purge_page BIGINT NOT NULL DEFAULT 0 CHECK (purge_page >= 0),
 purge_cursor TEXT,
 purge_truncated BOOLEAN NOT NULL DEFAULT FALSE,
 purge_started BOOLEAN NOT NULL DEFAULT FALSE,
 provider_quiesced_at DOUBLE PRECISION,
 provider_quiescence_sha256 TEXT,
 purge_complete BOOLEAN NOT NULL DEFAULT FALSE,
 purge_deleted_objects BIGINT NOT NULL DEFAULT 0 CHECK (purge_deleted_objects >= 0),
 purge_inventory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (purge_inventory_bytes >= 0),
 purge_inventory_objects BIGINT NOT NULL DEFAULT 0 CHECK (purge_inventory_objects >= 0)
);

CREATE TABLE IF NOT EXISTS artifact_operations (
 environment TEXT NOT NULL,
 operation_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('put','get','purge_list','purge_delete')),
 object_key TEXT,
 sha256 TEXT,
 size BIGINT NOT NULL DEFAULT 0 CHECK (size >= 0),
 audience TEXT,
 media_type TEXT,
 status TEXT NOT NULL CHECK (status IN ('reserved','in_flight','unknown','committed','failed')),
 attempts BIGINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
 attempt_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 cursor TEXT,
 object_keys TEXT,
 page_result TEXT,
 response_bytes BIGINT NOT NULL DEFAULT 0 CHECK (response_bytes >= 0),
 created DOUBLE PRECISION NOT NULL,
 updated DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(environment, operation_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS artifact_put_object_key
 ON artifact_operations(environment, object_key) WHERE kind='put';
CREATE INDEX IF NOT EXISTS artifact_operations_pending
 ON artifact_operations(environment, status, kind);

CREATE TABLE IF NOT EXISTS hosted_branch_copies (
 environment TEXT PRIMARY KEY,
 parent TEXT NOT NULL,
 checkpoint_sha256 TEXT NOT NULL,
 turns BIGINT,
 status TEXT NOT NULL CHECK (status IN ('pending','completed')),
 created DOUBLE PRECISION NOT NULL,
 updated DOUBLE PRECISION NOT NULL
);
