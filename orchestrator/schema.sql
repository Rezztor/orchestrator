-- Idempotent: run at every start by the owner role (db.migrate).
-- Tenant isolation is structural: every tenant table forces row-level security, and a session that has not set
-- app.tenant_id sees and writes nothing. The application role (orch_app) can never bypass it.

CREATE TABLE IF NOT EXISTS tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    role text NOT NULL CHECK (role IN ('tenant-admin', 'approver', 'operator', 'viewer')),
    UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id),
    prompt text NOT NULL,
    state text NOT NULL CHECK (state IN ('INTAKE', 'DISCOVER', 'PLAN', 'POLICY', 'APPROVE', 'EXECUTE', 'VERIFY',
                                         'ROLLBACK', 'REPORT', 'DONE', 'FAILED')),
    answer text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS devices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    address text NOT NULL,
    platform text NOT NULL,
    site text NOT NULL DEFAULT '',
    tags text[] NOT NULL DEFAULT '{}',
    UNIQUE (tenant_id, name)
);

-- Append-only, hash-chained per tenant. Each row's hash covers the previous row's hash and its own content, so an
-- edit or a deleted row anywhere breaks every hash after it (db.verify_audit).
CREATE TABLE IF NOT EXISTS audit (
    seq bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor text NOT NULL,
    kind text NOT NULL,
    data jsonb NOT NULL DEFAULT '{}',
    prev_hash text NOT NULL DEFAULT '',
    hash text NOT NULL DEFAULT ''
);

CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- One writer per tenant at a time, so two inserts cannot chain to the same predecessor.
    PERFORM pg_advisory_xact_lock(hashtext('audit:' || NEW.tenant_id::text));
    SELECT hash INTO NEW.prev_hash FROM audit WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    NEW.prev_hash := coalesce(NEW.prev_hash, '');
    NEW.at := date_trunc('milliseconds', NEW.at);
    NEW.hash := encode(sha256(convert_to(concat_ws('|', NEW.prev_hash, NEW.tenant_id::text,
        to_char(NEW.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS'), NEW.actor, NEW.kind, NEW.data::text),
        'UTF8')), 'hex');
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION audit_refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the audit ledger is append-only';
END $$;

CREATE OR REPLACE TRIGGER audit_chain BEFORE INSERT ON audit FOR EACH ROW EXECUTE FUNCTION audit_chain();
CREATE OR REPLACE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit
    FOR EACH ROW EXECUTE FUNCTION audit_refuse_change();
CREATE OR REPLACE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit
    FOR EACH STATEMENT EXECUTE FUNCTION audit_refuse_change();

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'requests', 'devices', 'audit'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_scope ON %I', t);
        EXECUTE format($p$CREATE POLICY tenant_scope ON %I
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
    END LOOP;
    -- Not forced: tenants are created by the owner role, which is the installation's administrator.
    ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_scope ON tenants;
    CREATE POLICY tenant_scope ON tenants USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid);
END $$;

GRANT SELECT ON tenants TO orch_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, requests, devices TO orch_app;
GRANT SELECT, INSERT ON audit TO orch_app;
GRANT USAGE ON SEQUENCE audit_seq_seq TO orch_app;
