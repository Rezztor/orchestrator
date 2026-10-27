# 0010 Free and open source for now: images on GHCR and a one-command install

**Decided by** RIfky, 2026-10-27. **Status** accepted. Supersedes the paid subscription in [0001](0001-clean-room-agpl.md).

Nothing is sold for now and no licence is issued: the Orchestrator is the main product on rezztor.com, open source
under the AGPL-3.0, and anyone may run it. A pushed tag `vX.Y.Z` runs the tests and publishes the images to
`ghcr.io/rezztor/orchestrator-{core,ansible,tofu,postgres}` from GitHub's hosted runners
(`.github/workflows/release.yml`). `deploy/install.sh`, fetched with one `curl | bash`, generates the secrets and a
self-signed certificate, starts the Compose stack and makes the first administrator. The signed offline release
(0007) stays for sites with no internet.

**Why:** a customer should go from nothing to a signed-in administrator with one command and no account with us.
**Cost:** `curl | bash` trusts GitHub and this repository at install time; a careful customer reads the script first
or uses the signed offline release. The images are linux/amd64 only, and the hosted runners see the tag's code, so
nothing secret may ever be in the repository.
