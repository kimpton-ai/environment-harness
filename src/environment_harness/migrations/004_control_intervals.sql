CREATE TABLE IF NOT EXISTS control_intervals (
 environment TEXT NOT NULL, id TEXT NOT NULL, sequence BIGINT NOT NULL,
 revision BIGINT NOT NULL, operation_id TEXT NOT NULL, operation_request_hash TEXT NOT NULL,
 runtime_identity TEXT NOT NULL, runtime_identity_hash TEXT NOT NULL,
 lease_owner TEXT NOT NULL, lease_epoch BIGINT NOT NULL,
 start_checkpoint TEXT NOT NULL, simulated_seconds DOUBLE PRECISION NOT NULL,
 controller_grant TEXT NOT NULL, status TEXT NOT NULL,
 control_log_digest TEXT, end_checkpoint TEXT, measurements TEXT, receipt TEXT,
 created DOUBLE PRECISION NOT NULL, sealed DOUBLE PRECISION, committed DOUBLE PRECISION,
 PRIMARY KEY(environment,id), UNIQUE(environment,sequence), UNIQUE(environment,operation_id));
CREATE INDEX IF NOT EXISTS control_intervals_status ON control_intervals(environment,status,sequence);
CREATE TABLE IF NOT EXISTS control_grants (
 environment TEXT NOT NULL, id TEXT NOT NULL, interval_id TEXT NOT NULL,
 controller TEXT NOT NULL, participant TEXT, generation BIGINT NOT NULL,
 lease_epoch BIGINT NOT NULL, expires DOUBLE PRECISION NOT NULL, requested_ttl DOUBLE PRECISION NOT NULL,
 last_sequence BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(environment,id), UNIQUE(environment,interval_id));
CREATE TABLE IF NOT EXISTS control_inputs (
 environment TEXT NOT NULL, interval_id TEXT NOT NULL, batch_id TEXT NOT NULL,
 grant_id TEXT NOT NULL, sequence BIGINT NOT NULL, request TEXT NOT NULL,
 request_hash TEXT NOT NULL, acknowledgement TEXT NOT NULL, accepted DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(environment,batch_id), UNIQUE(environment,interval_id,sequence));
DROP TRIGGER IF EXISTS immutable_control_inputs ON control_inputs;
CREATE TRIGGER immutable_control_inputs BEFORE UPDATE OR DELETE ON control_inputs
 FOR EACH ROW EXECUTE FUNCTION immutable_evidence();
