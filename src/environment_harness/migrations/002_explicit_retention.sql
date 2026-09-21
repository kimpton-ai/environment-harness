-- Only the trusted database-owning retention caller can opt into tenant erasure.
-- Ordinary session operations never set this transaction-local value.
CREATE OR REPLACE FUNCTION immutable_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' AND EXISTS(
  SELECT 1 FROM environments WHERE id=OLD.environment
   AND tenant=nullif(current_setting('environment_harness.erase_tenant',true),'')
 ) THEN RETURN OLD; END IF;
 RAISE EXCEPTION 'immutable evidence';
END $$;
