#!/bin/sh
set -eu

: "${DAYFINCH_BACKUP_DESTINATION:?Set an absolute backup destination}"

case "$DAYFINCH_BACKUP_DESTINATION" in
  /*) ;;
  *) echo "DAYFINCH_BACKUP_DESTINATION must be an absolute path" >&2; exit 2 ;;
esac

umask 077
mkdir -p "$DAYFINCH_BACKUP_DESTINATION"
archive="/backups/dayfinch-$(date -u +%Y%m%dT%H%M%SZ).dfbackup"

compose() {
  if [ -n "${DAYFINCH_DEPLOYMENT_COMPOSE_FILE:-}" ]; then
    docker compose -f compose.yaml -f "$DAYFINCH_DEPLOYMENT_COMPOSE_FILE" \
      -f compose.backup.yaml "$@"
  else
    docker compose -f compose.yaml -f compose.backup.yaml "$@"
  fi
}

restore_server() {
  compose up -d --wait --wait-timeout 90 dayfinch-server >/dev/null
}

compose stop dayfinch-server
trap restore_server EXIT HUP INT TERM
compose run --rm dayfinch-ops backup --maintenance-confirmed "$archive"
restore_server
trap - EXIT HUP INT TERM
