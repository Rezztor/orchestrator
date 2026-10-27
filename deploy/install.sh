#!/usr/bin/env bash
# Rezztor Orchestrator on one host, in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/Rezztor/orchestrator/main/deploy/install.sh | bash
#
# Needs Docker with the compose plugin, curl and openssl. Writes ./rezztor-orchestrator (ORCH_DIR) with generated
# passwords and tokens, a self-signed certificate, compose.yaml and .env; pulls the images from ghcr.io/rezztor;
# starts them; makes the first organisation and its administrator and prints the sign-in. Set these first to skip
# the questions: ORCH_MODEL_URL (any OpenAI-compatible endpoint ending in /v1), ORCH_MODEL, ORCH_MODEL_KEY,
# ORCH_ORG (default acme), ORCH_ADMIN (default admin), ORCH_HTTPS_PORT (default 443), ORCH_VERSION.
set -euo pipefail
version=${ORCH_VERSION:-0.7.0}
dir=${ORCH_DIR:-$PWD/rezztor-orchestrator}
port=${ORCH_HTTPS_PORT:-443}
org=${ORCH_ORG:-acme}
admin=${ORCH_ADMIN:-admin}
src=${ORCH_SOURCE:-https://raw.githubusercontent.com/Rezztor/orchestrator/v$version/deploy}

for tool in docker curl openssl; do
    command -v "$tool" >/dev/null || { echo "error: $tool is needed" >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "error: the docker compose plugin is needed" >&2; exit 1; }
[[ ! -e $dir/.env ]] || { echo "error: $dir is already installed (upgrade: set ORCH_VERSION in its .env, then" \
    "docker compose pull && docker compose up -d)" >&2; exit 1; }

ask() {  # ask NAME QUESTION: keep $NAME if set, else read it from the terminal (stdin is this script under curl | bash)
    if [[ -z ${!1:-} ]] && { : </dev/tty; } 2>/dev/null; then read -rp "$2" "$1" </dev/tty || true; fi
}
ask ORCH_MODEL_URL "Model endpoint, OpenAI-compatible, ending in /v1 (empty to set it later): "
[[ -z ${ORCH_MODEL_URL:-} ]] || ask ORCH_MODEL "Model name: "
[[ -z ${ORCH_MODEL_URL:-} || $ORCH_MODEL_URL == http://* ]] || ask ORCH_MODEL_KEY "API key for it (empty if none): "

mkdir -p "$dir" && chmod 700 "$dir" && cd "$dir"
curl -fsSL "$src/compose.yaml" -o compose.yaml
mkdir -p certs keys mcp ansible/playbooks tofu-modules
[[ -e ansible/inventory ]] || printf 'all:\n  hosts: {}\n' > ansible/inventory  # add devices here (Ansible YAML)

rand() { openssl rand -hex 24; }
host=$(hostname -f 2>/dev/null || hostname)
public="https://$host$([[ $port == 443 ]] || echo ":$port")"
umask 077
cat > .env <<EOF
ORCH_VERSION=$version
ORCH_PUBLIC_URL=$public
ORCH_HTTPS_PORT=$port
ORCH_DB_OWNER_PASSWORD=$(rand)
ORCH_DB_APP_PASSWORD=$(rand)
ORCH_ANSIBLE_TOKEN=$(rand)
ORCH_TOFU_TOKEN=$(rand)
ORCH_MODEL_URL=${ORCH_MODEL_URL:-}
ORCH_MODEL=${ORCH_MODEL:-}
ORCH_MODEL_KEY=${ORCH_MODEL_KEY:-}
ORCH_SIEM_FORMAT=json
EOF
# the containers run as their own user, so the certificate must be readable inside them; $dir itself stays 700
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 -subj "/CN=$host" \
    -addext "subjectAltName=DNS:$host,DNS:localhost,IP:127.0.0.1" -keyout certs/tls.key -out certs/tls.crt 2>/dev/null
chmod 755 certs ansible ansible/playbooks && chmod 644 certs/tls.key certs/tls.crt ansible/inventory

docker compose up -d
for _ in $(seq 60); do
    [[ $(docker compose ps --format '{{.Health}}' web 2>/dev/null) == healthy ]] && break
    sleep 2
done
[[ $(docker compose ps --format '{{.Health}}' web) == healthy ]] || { docker compose logs --tail 30 web >&2
    echo "error: the web service did not become healthy" >&2; exit 1; }

orch() { docker compose run --rm -T migrate python -m orchestrator "$@"; }
tenant=$(orch tenant-add "$org" "$admin" | tail -n 1)
password=$(rand)
printf '%s\n' "$password" | orch set-password "$tenant" "$admin" 2>/dev/null
echo "$tenant" > tenant-id

cat <<EOF

Rezztor Orchestrator $version is running in $dir.

  Sign in:       $public  (or https://localhost$([[ $port == 443 ]] || echo ":$port") on this host)
  Organisation:  $org
  User:          $admin
  Password:      $password   (shown once; keep it somewhere safe)

The certificate is self-signed: put your own in certs/tls.crt and certs/tls.key, then docker compose restart web.
Devices go in ansible/inventory and reviewed playbooks in ansible/playbooks/.
$([[ -n ${ORCH_MODEL_URL:-} ]] || echo "No model yet: set ORCH_MODEL_URL and ORCH_MODEL in .env, then docker compose up -d.")
Administration, from $dir: docker compose run --rm migrate python -m orchestrator COMMAND (README lists them)
EOF
