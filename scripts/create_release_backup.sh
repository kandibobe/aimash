#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly RELEASE_VERSION="v3.0.0"
readonly RELEASE_TAG="v3.0.0-stable"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly RELEASE_DATE="${RELEASE_DATE:-$(date -u +%Y%m%d)}"
readonly BACKUP_DIR="${PROJECT_ROOT}/backups"
readonly ARCHIVE_NAME="release_v3.0.0_${RELEASE_DATE}.tar.gz"
readonly ARCHIVE_PATH="${BACKUP_DIR}/${ARCHIVE_NAME}"
readonly PARTIAL_PATH="${ARCHIVE_PATH}.partial"
readonly LOCK_DIR="${BACKUP_DIR}/.release_v3.0.0.lock"

STAGE_DIR=""
LOCK_HELD=0

log() {
  printf '[release-backup] %s\n' "$*"
}

die() {
  printf '[release-backup] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

cleanup() {
  if [[ -n "${STAGE_DIR}" && -d "${STAGE_DIR}" ]]; then
    case "${STAGE_DIR}" in
      "${TMPDIR:-/tmp}"/aimash-release-v3.0.0.*) rm -rf -- "${STAGE_DIR}" ;;
      *) printf '[release-backup] WARNING: refused to remove unexpected temp path: %s\n' "${STAGE_DIR}" >&2 ;;
    esac
  fi
  if [[ "${LOCK_HELD}" == "1" && "${LOCK_DIR}" == "${BACKUP_DIR}/.release_v3.0.0.lock" ]]; then
    [[ ! -e "${PARTIAL_PATH}" ]] || rm -f -- "${PARTIAL_PATH}"
    rmdir -- "${LOCK_DIR}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

require_command docker
require_command git
require_command tar
require_command grep

git -C "${PROJECT_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "project root is not a Git worktree: ${PROJECT_ROOT}"

if [[ -n "$(git -C "${PROJECT_ROOT}" status --porcelain --untracked-files=normal)" ]]; then
  if [[ "${ALLOW_DIRTY:-0}" != "1" ]]; then
    die "Git worktree is dirty. Commit the release files first, or set ALLOW_DIRTY=1 for an emergency snapshot."
  fi
  log "WARNING: creating an emergency snapshot from a dirty worktree (ALLOW_DIRTY=1)"
fi

docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"

postgres_container="${POSTGRES_CONTAINER:-}"
if [[ -z "${postgres_container}" ]] && docker compose version >/dev/null 2>&1; then
  postgres_container="$(
    cd -- "${PROJECT_ROOT}"
    docker compose ps -q postgres 2>/dev/null || true
  )"
fi
if [[ -z "${postgres_container}" ]]; then
  postgres_container="aimash-pg"
fi

docker inspect "${postgres_container}" >/dev/null 2>&1 \
  || die "PostgreSQL container not found. Start the Compose service 'postgres' or set POSTGRES_CONTAINER."

[[ "$(docker inspect --format '{{.State.Running}}' "${postgres_container}")" == "true" ]] \
  || die "PostgreSQL container is not running: ${postgres_container}"

postgres_health="$(
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "${postgres_container}"
)"
if [[ "${postgres_health}" != "healthy" && "${postgres_health}" != "none" ]]; then
  die "PostgreSQL container health is '${postgres_health}', expected 'healthy'"
fi

docker exec "${postgres_container}" sh -ec \
  'command -v pg_dump >/dev/null; command -v pg_restore >/dev/null; test -n "${POSTGRES_USER:-}"; test -n "${POSTGRES_DB:-}"' \
  || die "pg_dump/pg_restore or POSTGRES_USER/POSTGRES_DB is unavailable in the PostgreSQL container"

mkdir -p -- "${BACKUP_DIR}"
mkdir -- "${LOCK_DIR}" 2>/dev/null \
  || die "another release backup is running, or a stale lock exists: ${LOCK_DIR}"
LOCK_HELD=1

[[ ! -e "${ARCHIVE_PATH}" ]] \
  || die "release archive already exists (refusing to overwrite): ${ARCHIVE_PATH}"
[[ ! -e "${PARTIAL_PATH}" ]] \
  || die "partial release archive already exists (refusing to overwrite): ${PARTIAL_PATH}"

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aimash-release-v3.0.0.XXXXXX")"
mkdir -p -- "${STAGE_DIR}/release-assets/postgres"
readonly DUMP_PATH="${STAGE_DIR}/release-assets/postgres/aimash.dump"

log "creating PostgreSQL schema+data dump from Compose service 'postgres'"
docker exec "${postgres_container}" sh -ec \
  'exec pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  > "${DUMP_PATH}"

[[ -s "${DUMP_PATH}" ]] || die "pg_dump produced an empty file"
docker exec -i "${postgres_container}" pg_restore --list < "${DUMP_PATH}" >/dev/null \
  || die "pg_restore could not read the generated dump"

git_commit="$(git -C "${PROJECT_ROOT}" rev-parse HEAD)"
git_dirty="false"
[[ -z "$(git -C "${PROJECT_ROOT}" status --porcelain --untracked-files=normal)" ]] || git_dirty="true"
cat > "${STAGE_DIR}/release-assets/MANIFEST.txt" <<EOF
release_version=${RELEASE_VERSION}
release_tag=${RELEASE_TAG}
created_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
git_commit=${git_commit}
git_dirty=${git_dirty}
database_dump=release-assets/postgres/aimash.dump
database_format=PostgreSQL custom archive (schema and data)
EOF

archive_rel="./backups/${ARCHIVE_NAME}"
partial_rel="${archive_rel}.partial"

log "archiving project to ${ARCHIVE_PATH}"
tar \
  --create \
  --gzip \
  --file "${PARTIAL_PATH}" \
  --directory "${PROJECT_ROOT}" \
  --exclude='./.git' \
  --exclude='./.git/*' \
  --exclude='*/.git' \
  --exclude='*/.git/*' \
  --exclude='__pycache__' \
  --exclude='*/__pycache__' \
  --exclude='*/__pycache__/*' \
  --exclude='.venv' \
  --exclude='*/.venv' \
  --exclude='*/.venv/*' \
  --exclude='venv' \
  --exclude='*/venv' \
  --exclude='*/venv/*' \
  --exclude="${archive_rel}" \
  --exclude="${partial_rel}" \
  --exclude='./backups/.release_v3.0.0.lock' \
  . \
  --directory "${STAGE_DIR}" \
  release-assets

tar -tzf "${PARTIAL_PATH}" > "${STAGE_DIR}/archive.list"
grep -Fxq 'release-assets/postgres/aimash.dump' "${STAGE_DIR}/archive.list" \
  || die "archive verification failed: PostgreSQL dump is missing"
grep -Fxq 'release-assets/MANIFEST.txt' "${STAGE_DIR}/archive.list" \
  || die "archive verification failed: release manifest is missing"

mv -- "${PARTIAL_PATH}" "${ARCHIVE_PATH}"
chmod 600 "${ARCHIVE_PATH}"

log "backup complete: ${ARCHIVE_PATH}"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${ARCHIVE_PATH}"
fi

printf '\nThe archive can contain production data and local secrets; keep it encrypted and off-site.\n'
printf 'After reviewing the backup and committing the release state, create and push the tag:\n\n'
printf '  cd %q\n' "${PROJECT_ROOT}"
printf '  git status --short\n'
printf '  git tag -a %q -m %q\n' "${RELEASE_TAG}" "HERMES 3.0 stable release"
printf '  git push origin %q\n' "${RELEASE_TAG}"
