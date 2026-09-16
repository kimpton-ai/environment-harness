
CREATE TABLE IF NOT EXISTS environments (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, manifest TEXT NOT NULL,
 state TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 0, scheduler TEXT NOT NULL,
 rng TEXT NOT NULL, participants TEXT NOT NULL, cursors TEXT NOT NULL,
 status TEXT NOT NULL, lineage TEXT NOT NULL, parent TEXT, checkpoint TEXT,
 lease_owner TEXT, lease_epoch BIGINT NOT NULL DEFAULT 0, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
 spent BIGINT NOT NULL DEFAULT 0, reserved BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
 environment TEXT NOT NULL, seq BIGINT NOT NULL, revision BIGINT NOT NULL,
 kind TEXT NOT NULL, body TEXT NOT NULL, audience TEXT NOT NULL, event_time DOUBLE PRECISION,
 ingested DOUBLE PRECISION NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(environment,seq));
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, environment TEXT NOT NULL, participant TEXT NOT NULL,
 generation BIGINT NOT NULL, revision BIGINT NOT NULL, body TEXT NOT NULL,
 UNIQUE(environment,participant,generation,revision));
CREATE TABLE IF NOT EXISTS actions (
 environment TEXT NOT NULL, id TEXT NOT NULL, participant TEXT NOT NULL,
 revision BIGINT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 receipt TEXT NOT NULL, PRIMARY KEY(environment,id));
CREATE UNIQUE INDEX IF NOT EXISTS one_action_per_phase ON actions(environment,participant,revision)
 WHERE status IN ('accepted','committed');
CREATE TABLE IF NOT EXISTS checkpoints (
 id TEXT PRIMARY KEY, environment TEXT NOT NULL, revision BIGINT NOT NULL,
 body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
 environment TEXT NOT NULL, id TEXT NOT NULL, participant TEXT NOT NULL,
 generation BIGINT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 receipt TEXT, reservation BIGINT NOT NULL, PRIMARY KEY(environment,id));
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, environment TEXT NOT NULL, audience TEXT NOT NULL,
 sha256 TEXT NOT NULL, size BIGINT NOT NULL, media_type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
 environment TEXT NOT NULL, revision BIGINT NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(environment,revision));
CREATE TABLE IF NOT EXISTS artifact_aliases (
 environment TEXT NOT NULL, alias TEXT NOT NULL, artifact TEXT NOT NULL, PRIMARY KEY(environment,alias));
CREATE TABLE IF NOT EXISTS credentials (
 hash TEXT PRIMARY KEY, principal TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL, revoked BIGINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS splits (
 tenant TEXT NOT NULL, environment TEXT NOT NULL, scenario TEXT NOT NULL,
 time_boundary TEXT NOT NULL, split TEXT NOT NULL,
 PRIMARY KEY(tenant,environment,scenario,time_boundary));
CREATE TABLE IF NOT EXISTS transitions (
 environment TEXT NOT NULL, revision BIGINT NOT NULL, input_hash TEXT NOT NULL,
 request TEXT NOT NULL, lease_epoch BIGINT NOT NULL, status TEXT NOT NULL,
 result TEXT, rng TEXT, PRIMARY KEY(environment,revision,input_hash));
CREATE TABLE IF NOT EXISTS agent_work (
 environment TEXT NOT NULL, revision BIGINT NOT NULL, participant TEXT NOT NULL,
 generation BIGINT NOT NULL, id TEXT NOT NULL, observation TEXT NOT NULL,
 status TEXT NOT NULL, response TEXT, agent_state TEXT,
 PRIMARY KEY(environment,revision,participant,generation));
CREATE OR REPLACE FUNCTION immutable_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable evidence'; END; $$;
DROP TRIGGER IF EXISTS immutable_events ON events;
CREATE TRIGGER immutable_events BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION immutable_evidence();
DROP TRIGGER IF EXISTS immutable_reports ON reports;
CREATE TRIGGER immutable_reports BEFORE UPDATE OR DELETE ON reports FOR EACH ROW EXECUTE FUNCTION immutable_evidence();
DROP TRIGGER IF EXISTS immutable_checkpoints ON checkpoints;
CREATE TRIGGER immutable_checkpoints BEFORE UPDATE OR DELETE ON checkpoints FOR EACH ROW EXECUTE FUNCTION immutable_evidence();
