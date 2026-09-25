-- Replace the removed four-role Principal credential row with a server-owned
-- access policy plus its resource constraints.
--
-- Every pre-0.3.0rc1 row persists the discarded role taxonomy and cannot be
-- reinterpreted as a policy, so this migration deletes all of them inside one
-- transaction. After upgrade, old bearer tokens return 401 and callers must
-- reissue through `environment-harness token`, the embedding API, or the
-- participant-credential operation. See docs/AUTHENTICATION.md.
DROP TABLE IF EXISTS credentials;
CREATE TABLE credentials (
 hash TEXT PRIMARY KEY,
 tenant TEXT NOT NULL,
 subject TEXT NOT NULL,
 policy TEXT NOT NULL,
 session TEXT,
 participant TEXT,
 generation BIGINT NOT NULL DEFAULT 0,
 expires DOUBLE PRECISION NOT NULL,
 revoked BIGINT NOT NULL DEFAULT 0);
