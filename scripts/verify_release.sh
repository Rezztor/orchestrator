#!/usr/bin/env bash
# Check a release folder before loading it: the signature on SHA256SUMS, then every file against it.
#
#   verify_release.sh RELEASE_DIR PUBLIC_KEY      then: docker load -i RELEASE_DIR/orchestrator-*-images.tar.gz
#
# PUBLIC_KEY is Rezztor's Ed25519 release key (PEM), obtained separately from the release, never from inside it.
set -euo pipefail
dir=${1:?usage: verify_release.sh RELEASE_DIR PUBLIC_KEY}
pub=${2:?usage: verify_release.sh RELEASE_DIR PUBLIC_KEY}
openssl pkeyutl -verify -pubin -inkey "$pub" -rawin -in "$dir/SHA256SUMS" -sigfile "$dir/SHA256SUMS.sig" >/dev/null \
    || { echo "BAD SIGNATURE: do not install this release" >&2; exit 1; }
listed=$(cut -d' ' -f3- "$dir/SHA256SUMS" | sort)
present=$(cd "$dir" && ls -1 | grep -vx -e SHA256SUMS -e SHA256SUMS.sig | sort)
[[ $listed == "$present" ]] || { echo "the folder holds files SHA256SUMS does not list, or lacks some" >&2; exit 1; }
(cd "$dir" && sha256sum --quiet --strict -c SHA256SUMS) || { echo "A FILE DOES NOT MATCH: do not install" >&2; exit 1; }
echo "release verified"
