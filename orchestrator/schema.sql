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

ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;  -- null: this user cannot sign in with a password

-- A signed-in browser. Only the SHA-256 of the cookie's token is stored.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);

ALTER TABLE requests DROP CONSTRAINT IF EXISTS requests_state_check;
ALTER TABLE requests ADD CONSTRAINT requests_state_check CHECK (state IN ('INTAKE', 'DISCOVER', 'PLAN', 'POLICY',
    'APPROVE', 'EXECUTE', 'VERIFY', 'ROLLBACK', 'REPORT', 'DONE', 'FAILED', 'REFUSED'));
ALTER TABLE requests ADD COLUMN IF NOT EXISTS tokens integer NOT NULL DEFAULT 0;

-- A tenant's own policy (YAML, see policy.py). It can only narrow the shipped one.
CREATE TABLE IF NOT EXISTS policies (
    tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
    document text NOT NULL,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
);

-- A row here stops all agent work in the tenant until it is removed.
CREATE TABLE IF NOT EXISTS kill_switch (
    tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
    reason text NOT NULL,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
);

-- The installation-wide stop: the owner role sets it, the application only reads it.
CREATE TABLE IF NOT EXISTS installation_kill (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    reason text NOT NULL,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
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

ALTER TABLE devices ADD COLUMN IF NOT EXISTS critical boolean NOT NULL DEFAULT false;  -- raises a change's risk

-- A proposed change: the capability's dry run, its digest and its lifecycle. People approve the digest; the run sends
-- it back to the executor, which re-plans and refuses on any difference.
CREATE TABLE IF NOT EXISTS changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    request_id uuid NOT NULL REFERENCES requests(id),
    requested_by uuid NOT NULL REFERENCES users(id),
    capability text NOT NULL,
    args jsonb NOT NULL,
    plan jsonb NOT NULL,          -- what people are shown, redacted
    digest text NOT NULL,         -- what they approve
    risk text NOT NULL CHECK (risk IN ('low', 'medium', 'high', 'destructive')),
    approvals_needed integer NOT NULL CHECK (approvals_needed > 0),
    state text NOT NULL DEFAULT 'PLANNED' CHECK (state IN ('PLANNED', 'APPROVED', 'REJECTED', 'EXECUTING',
                                                           'VERIFIED', 'ROLLED_BACK', 'SUPERSEDED', 'FAILED')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One decision per person per change, bound to the digest they saw.
CREATE TABLE IF NOT EXISTS approvals (
    change_id uuid NOT NULL REFERENCES changes(id),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id),
    digest text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approve', 'reject')),
    reason text,
    at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (change_id, user_id)
);

ALTER TABLE changes ADD COLUMN IF NOT EXISTS review text;  -- the reviewer agent's notes, advice only

-- A tenant's own skills. One the agent proposes is inert until a tenant administrator approves it.
CREATE TABLE IF NOT EXISTS skills (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    description text NOT NULL,
    body text NOT NULL,
    capabilities text[] NOT NULL DEFAULT '{}',
    source text NOT NULL CHECK (source IN ('tenant', 'induced')),
    state text NOT NULL CHECK (state IN ('proposed', 'approved', 'retired')),
    proposed_by text NOT NULL,
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name)
);

-- A saved prompt with {placeholders}; a schedule runs one as a named user every so many minutes.
CREATE TABLE IF NOT EXISTS recipes (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    description text NOT NULL,
    prompt text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    recipe text NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    run_as text NOT NULL,
    every_minutes integer NOT NULL CHECK (every_minutes >= 5),
    next_run_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    FOREIGN KEY (tenant_id, recipe) REFERENCES recipes (tenant_id, name) ON DELETE CASCADE
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'requests', 'devices', 'audit', 'policies', 'kill_switch', 'changes',
                            'approvals', 'skills', 'recipes', 'schedules'] LOOP
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

    -- Not forced either: session_tenant() below reads it as the owner, before any tenant is known.
    ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_scope ON sessions;
    CREATE POLICY tenant_scope ON sessions
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
END $$;

-- The two lookups that happen before a tenant is known. Each returns one id and nothing else.
CREATE OR REPLACE FUNCTION tenant_id_by_name(p_name text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $$ SELECT id FROM tenants WHERE name = p_name $$;
CREATE OR REPLACE FUNCTION session_tenant(p_token_hash text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $$ SELECT tenant_id FROM sessions WHERE token_hash = p_token_hash AND expires_at > now() $$;
-- The scheduler's question across tenants: which schedules are due. It learns ids only, then works inside each tenant.
CREATE OR REPLACE FUNCTION due_schedules() RETURNS TABLE (tenant_id uuid, id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
    AS $$ SELECT tenant_id, id FROM schedules WHERE next_run_at <= now() ORDER BY next_run_at $$;
REVOKE ALL ON FUNCTION tenant_id_by_name(text), session_tenant(text), due_schedules() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant_id_by_name(text), session_tenant(text), due_schedules() TO orch_app;

GRANT SELECT ON tenants, installation_kill TO orch_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, requests, devices, sessions, policies, kill_switch, changes, skills, recipes,
    schedules TO orch_app;
GRANT SELECT, INSERT ON approvals TO orch_app;
GRANT SELECT, INSERT ON audit TO orch_app;
GRANT USAGE ON SEQUENCE audit_seq_seq TO orch_app;
