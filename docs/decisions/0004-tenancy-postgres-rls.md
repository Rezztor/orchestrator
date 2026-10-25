# 0004 Tenants in PostgreSQL with forced row-level security

**Status** accepted, 2026-10-25.

Every tenant table carries `tenant_id` and `FORCE ROW LEVEL SECURITY` keyed on `app.tenant_id`. The application role
`orch_app` cannot bypass it; the owner role only migrates, creates tenants and forwards the ledger to the SIEM. The few
lookups that must happen before a tenant is known are `SECURITY DEFINER` functions that return ids only.

**Why:** isolation that holds even when application code forgets a `WHERE`. **Cost:** cross-tenant work (the
scheduler, anchors, the lost-run sweeper) goes through narrow definer functions and then works tenant by tenant.
