CREATE TABLE IF NOT EXISTS experiments (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
 seed BIGINT NOT NULL, trials BIGINT NOT NULL, total BIGINT NOT NULL,
 completed BIGINT NOT NULL DEFAULT 0, running BIGINT NOT NULL DEFAULT 0,
 queued BIGINT NOT NULL DEFAULT 0, failed BIGINT NOT NULL DEFAULT 0,
 config TEXT NOT NULL, error TEXT, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS scenario_snapshots (
 experiment TEXT NOT NULL, scenario TEXT NOT NULL, position BIGINT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(experiment,scenario));
CREATE TABLE IF NOT EXISTS session_runs (
 environment TEXT PRIMARY KEY, tenant TEXT NOT NULL, experiment TEXT, scenario TEXT NOT NULL,
 trial BIGINT NOT NULL, seed BIGINT NOT NULL, status TEXT NOT NULL, error TEXT,
 turns BIGINT NOT NULL DEFAULT 0, target_turns BIGINT NOT NULL,
 latest_activity TEXT, scenario_body TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS session_runs_experiment ON session_runs(experiment,status,scenario,trial);
CREATE TABLE IF NOT EXISTS event_outbox (
 id BIGSERIAL PRIMARY KEY, tenant TEXT NOT NULL, topic TEXT NOT NULL,
 experiment TEXT, environment TEXT, kind TEXT NOT NULL, body TEXT NOT NULL,
 created DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS event_outbox_scope ON event_outbox(tenant,id);
