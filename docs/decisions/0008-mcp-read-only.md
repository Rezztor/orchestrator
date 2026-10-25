# 0008 Customer MCP servers are read-only and bound to named tenants

**Status** accepted, 2026-10-25.

A customer can connect its own MCP servers through code-free manifests in `ORCH_MCP_PLUGINS`. Each capability is
`mcp.NAME.<...>`, read only, with parameters from the reviewed manifest (never the server's description), and the
manifest names the tenants that may use it.

**Why:** an MCP tool has no dry run or digest, so it cannot go through the change path, and a server shared between
tenants by accident would leak one tenant's data to another. **Cost:** changes through a customer's own systems need
an executor capability instead.
