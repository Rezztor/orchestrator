# 0009 A tenant administrator manages the tenant from the page; secrets stay with the installation's owner

**Status** accepted, 2026-10-26.

From the page a tenant administrator adds people and sets roles, resets an authenticator, adds devices and marks
them critical, edits the tenant's policy, and adds or removes webhooks and triggers. A tenant's executor and model
routes, directory sign-in, the SIEM, signing keys and the installation-wide stop stay on the command line with the
installation's owner. The page is plain JavaScript served by the web process, with no build step and nothing loaded
from elsewhere.

**Why:** a route or a directory setting names an environment variable that holds a server secret; a tenant
administrator who could set one could point that secret at a server of their own and read it. A role change ends the
person's sessions, so a new approver cannot skip the authenticator step on an old one. No build step means no
package supply chain behind the page and a strict `default-src 'self'` policy. **Cost:** connecting a tenant's own
executor, model or directory needs the installation's owner, and the page has no component library to grow from.
