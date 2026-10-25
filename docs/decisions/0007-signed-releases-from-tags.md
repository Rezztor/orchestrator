# 0007 Releases built from a tag in a fresh clone and signed with Ed25519

**Status** accepted, 2026-10-25.

`scripts/build_release.sh` clones the pushed tag, builds the four images (core, ansible, tofu, postgres), saves them
as one archive, writes `SHA256SUMS` and signs it with Rezztor's Ed25519 release key. The customer checks it with
`verify_release.sh` and Rezztor's public key, obtained separately, before `docker load`. Base images and OpenTofu are
pinned by digest or checksum. Nothing is fetched or installed at run time.

**Why:** an offline customer can prove a delivery came from Rezztor unchanged. **Cost:** the release key must be
kept off the build machine's repository and backed up; tags are not yet signature-checked.
