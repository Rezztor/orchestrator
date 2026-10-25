# 0006 Authenticator codes for approvers, even after directory sign-in

**Status** accepted, 2026-10-25.

Approvers and tenant administrators give a TOTP code after the password or the OpenID Connect directory. The
directory's own MFA is not trusted for this, because ID tokens do not reliably say whether it was used (Entra ID v2
tokens often omit `amr`).

**Why:** approving a change to production equipment is the most valuable thing an account can do here; one rule for
every sign-in path is easier to trust than one per directory. **Cost:** people whose directory already asked for MFA
are asked again by the orchestrator.
