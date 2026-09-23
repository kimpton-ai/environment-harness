CREATE TABLE IF NOT EXISTS trajectory_snapshots (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, environment TEXT NOT NULL,
 body TEXT NOT NULL, digest TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS trajectory_snapshot_records (
 snapshot TEXT NOT NULL, sequence BIGINT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(snapshot,sequence));
CREATE TABLE IF NOT EXISTS trajectory_sources (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, namespace TEXT NOT NULL, run_id TEXT NOT NULL,
 registration TEXT NOT NULL, registration_hash TEXT NOT NULL,
 collection_state TEXT NOT NULL, execution_state TEXT NOT NULL,
 termination TEXT NOT NULL, verified_outcome TEXT NOT NULL,
 acknowledged_position TEXT, acknowledged_hash TEXT,
 backlog BIGINT, gaps TEXT NOT NULL, capture_failures TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, UNIQUE(tenant,namespace,run_id));
CREATE TABLE IF NOT EXISTS trajectory_source_records (
 source TEXT NOT NULL, ordinal BIGINT NOT NULL, record_id TEXT NOT NULL, position TEXT NOT NULL,
 source_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, body TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(source,ordinal), UNIQUE(source,record_id), UNIQUE(source,position));
CREATE TABLE IF NOT EXISTS trajectory_datasets (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS training_runs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, dataset TEXT NOT NULL, body TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL);

CREATE OR REPLACE FUNCTION immutable_trajectory_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND OLD.tenant=nullif(current_setting('environment_harness.erase_tenant',true),'')
 THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable evidence';
END $$;
DROP TRIGGER IF EXISTS immutable_trajectory_snapshots ON trajectory_snapshots;
CREATE TRIGGER immutable_trajectory_snapshots BEFORE UPDATE OR DELETE ON trajectory_snapshots
 FOR EACH ROW EXECUTE FUNCTION immutable_trajectory_snapshot();

CREATE OR REPLACE FUNCTION immutable_trajectory_snapshot_record() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS(
  SELECT 1 FROM trajectory_snapshots WHERE id=OLD.snapshot
   AND tenant=nullif(current_setting('environment_harness.erase_tenant',true),'')
 ) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable evidence';
END $$;
DROP TRIGGER IF EXISTS immutable_trajectory_snapshot_records ON trajectory_snapshot_records;
CREATE TRIGGER immutable_trajectory_snapshot_records BEFORE UPDATE OR DELETE
 ON trajectory_snapshot_records FOR EACH ROW EXECUTE FUNCTION immutable_trajectory_snapshot_record();

CREATE OR REPLACE FUNCTION immutable_trajectory_source_record() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS(
  SELECT 1 FROM trajectory_sources WHERE id=OLD.source
   AND tenant=nullif(current_setting('environment_harness.erase_tenant',true),'')
 ) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable evidence';
END $$;
DROP TRIGGER IF EXISTS immutable_trajectory_source_records ON trajectory_source_records;
CREATE TRIGGER immutable_trajectory_source_records BEFORE UPDATE OR DELETE
 ON trajectory_source_records FOR EACH ROW EXECUTE FUNCTION immutable_trajectory_source_record();

CREATE OR REPLACE FUNCTION immutable_tenant_resource() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND OLD.tenant=nullif(current_setting('environment_harness.erase_tenant',true),'')
 THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable evidence';
END $$;
DROP TRIGGER IF EXISTS immutable_trajectory_datasets ON trajectory_datasets;
CREATE TRIGGER immutable_trajectory_datasets BEFORE UPDATE OR DELETE ON trajectory_datasets
 FOR EACH ROW EXECUTE FUNCTION immutable_tenant_resource();
DROP TRIGGER IF EXISTS immutable_training_runs ON training_runs;
CREATE TRIGGER immutable_training_runs BEFORE UPDATE OR DELETE ON training_runs
 FOR EACH ROW EXECUTE FUNCTION immutable_tenant_resource();
