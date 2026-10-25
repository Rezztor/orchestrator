# Third-party notices

Rezztor Orchestrator contains no code from other projects. The libraries it installs as dependencies are listed
here with their licences, added in the same change that adds the dependency.

| Library | Use | Licence |
|---|---|---|
| psycopg (with psycopg-binary, which bundles libpq) | PostgreSQL driver | LGPL-3.0 (libpq: PostgreSQL Licence) |
| PyYAML | Plugin manifests and policy data | MIT |
| ansible-core | Run by the Ansible executor as a separate program; our callback plugin runs inside it | GPL-3.0-or-later (compatible with the AGPL-3.0) |
| OpenTofu | Run by the cloud executor as a separate program, with the providers the customer installs | MPL-2.0 |
| pytest | Tests only, not shipped | MIT |
