-- Persist the portable environment reference on every Session row so a new
-- process can match it against the typed factories supplied to its harness.
-- Only `(id, version, spec_digest)` is serialized: recovery never imports
-- persisted text, and a missing, duplicate, or mismatched factory leaves the
-- Session durably blocked with an inspectable reason.
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS environment_id TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS environment_version TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS spec_digest TEXT;
ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS blocked_reason TEXT;
