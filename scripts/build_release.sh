#!/usr/bin/env bash
# Build a customer release from a pushed tag, in a fresh clone: never from a working folder.
#
#   scripts/build_release.sh TAG SIGNING_KEY [REPO]
#
# TAG is vX.Y.Z; SIGNING_KEY is the Ed25519 private key (PEM) kept off this repository; REPO defaults to the GitHub
# remote and may be any clone URL. Writes release-TAG/: the four images as one archive, compose.yaml, env.example,
# SHA256SUMS and SHA256SUMS.sig. A customer checks it with scripts/verify_release.sh before loading anything.
# shortcut: the tag itself is not signature-checked (no tag-signing key yet); add `git tag -v` when tags are signed.
set -euo pipefail
tag=${1:?usage: build_release.sh TAG SIGNING_KEY [REPO]}
key=${2:?usage: build_release.sh TAG SIGNING_KEY [REPO]}
repo=${3:-https://github.com/Rezztor/orchestrator.git}
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "TAG must look like v1.2.3" >&2; exit 2; }
version=${tag#v}
out=$PWD/release-$tag
[[ ! -e $out ]] || { echo "$out already exists" >&2; exit 2; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

git ls-remote --exit-code --tags "$repo" "refs/tags/$tag" >/dev/null || { echo "no tag $tag in $repo" >&2; exit 2; }
git clone --quiet --depth 1 --branch "$tag" "$repo" "$work/src"
commit=$(git -C "$work/src" rev-parse HEAD)

images=()
for target in core ansible tofu postgres; do
    image=ghcr.io/rezztor/orchestrator-$target:$version  # the names compose.yaml pulls, so a loaded archive needs no pull
    docker build --pull --target "$target" --label "org.opencontainers.image.version=$version" \
        --label "org.opencontainers.image.revision=$commit" -t "$image" "$work/src"
    images+=("$image")
done

mkdir -p "$out"
docker save "${images[@]}" | gzip -n > "$out/orchestrator-$version-images.tar.gz"
cp "$work/src/deploy/compose.yaml" "$work/src/deploy/env.example" "$work/src/scripts/verify_release.sh" "$out/"
printf '%s\n' "tag $tag" "commit $commit" "images ${images[*]}" > "$out/RELEASE"
(cd "$out" && sha256sum -- * > "$work/SHA256SUMS" && mv "$work/SHA256SUMS" SHA256SUMS)
openssl pkeyutl -sign -inkey "$key" -rawin -in "$out/SHA256SUMS" -out "$out/SHA256SUMS.sig"
echo "release $tag ($commit) in $out"
