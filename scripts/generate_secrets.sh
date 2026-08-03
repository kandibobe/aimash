#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly TEMPLATE_PATH="${PROJECT_ROOT}/.env.example"
readonly ENV_PATH="${PROJECT_ROOT}/.env"

TEMP_PATH=""

die() {
  printf '[generate-secrets] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${TEMP_PATH}" && -f "${TEMP_PATH}" ]]; then
    rm -f -- "${TEMP_PATH}"
  fi
}

trap cleanup EXIT INT TERM

command -v openssl >/dev/null 2>&1 || die "openssl is required"
command -v awk >/dev/null 2>&1 || die "awk is required"
[[ -f "${TEMPLATE_PATH}" ]] || die "template not found: ${TEMPLATE_PATH}"
[[ ! -e "${ENV_PATH}" ]] || die ".env already exists; refusing to overwrite or rotate live keys"

# All seeds come from OpenSSL's CSPRNG. HMAC keys may stay hex. Fernet requires exactly
# 32 bytes encoded as URL-safe Base64, so derive that representation from a 32-byte hex seed.
fernet_seed="$(openssl rand -hex 32)"
fernet_key="$(
  printf '%s' "${fernet_seed}" \
    | openssl dgst -sha256 -binary \
    | openssl base64 -A \
    | tr '+/' '-_'
)"
pseudonymization_key="$(openssl rand -hex 32)"
trust_key="$(openssl rand -hex 32)"
unset fernet_seed

TEMP_PATH="$(mktemp "${PROJECT_ROOT}/.env.tmp.XXXXXX")"
awk \
  -v fernet_key="${fernet_key}" \
  -v pseudonymization_key="${pseudonymization_key}" \
  -v trust_key="${trust_key}" '
    BEGIN { fernet_seen = 0; pseudonym_seen = 0; trust_seen = 0 }
    /^SECRETS_ENCRYPTION_KEY=/ {
      print "SECRETS_ENCRYPTION_KEY=" fernet_key
      fernet_seen++
      next
    }
    /^PSEUDONYMIZATION_HMAC_KEY=/ {
      print "PSEUDONYMIZATION_HMAC_KEY=" pseudonymization_key
      pseudonym_seen++
      next
    }
    /^AIMASH_TRUST_HMAC_KEY=/ {
      print "AIMASH_TRUST_HMAC_KEY=" trust_key
      trust_seen++
      next
    }
    { print }
    END {
      if (fernet_seen != 1 || pseudonym_seen != 1 || trust_seen != 1) {
        exit 42
      }
    }
  ' "${TEMPLATE_PATH}" > "${TEMP_PATH}" \
  || die "the template must contain each managed secret exactly once"

chmod 600 "${TEMP_PATH}"
mv -- "${TEMP_PATH}" "${ENV_PATH}"
TEMP_PATH=""
unset fernet_key pseudonymization_key trust_key

printf '[generate-secrets] created %s with mode 0600\n' "${ENV_PATH}"
printf '[generate-secrets] fill the remaining credentials before starting Compose\n'
