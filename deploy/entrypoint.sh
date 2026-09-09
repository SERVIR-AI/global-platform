#!/bin/sh
# Restore the receipt institution, then serve under continuous replication.
#
# FAILS CLOSED on both counts. An unauthenticated public server and a server
# whose receipts silently die on redeploy are the two ways this deployment can
# be quietly wrong, so neither is reachable by forgetting an env var — each
# needs an explicit opt-out you have to type on purpose.
set -eu

DB="${GRP_DB_PATH:-/app/cache/mcp/grp.db}"
mkdir -p "$(dirname "$DB")"

# FAILS CLOSED: the only ways to serve are OAuth on (AuthKit) or the explicit
# anonymous opt-out. A half-configured provider would publish discovery
# documents pointing nowhere and refuse every login, so when OAuth is on every
# value it needs must be present.
if [ "${GRP_OAUTH_ENABLED:-0}" != "1" ] && [ "${GRP_ALLOW_ANONYMOUS:-}" != "1" ]; then
  echo "FATAL: authentication is off — set GRP_OAUTH_ENABLED=1 (with the AuthKit" >&2
  echo "       settings below), or set GRP_ALLOW_ANONYMOUS=1 to serve with no gate." >&2
  exit 1
fi
if [ "${GRP_OAUTH_ENABLED:-0}" = "1" ]; then
  if [ -z "${GRP_AUTHKIT_DOMAIN:-}" ]; then
    echo "FATAL: GRP_OAUTH_ENABLED=1 but GRP_AUTHKIT_DOMAIN is unset." >&2
    exit 1
  fi
  if [ -z "${GRP_PUBLIC_URL:-}" ]; then
    echo "FATAL: GRP_OAUTH_ENABLED=1 but GRP_PUBLIC_URL is unset." >&2
    exit 1
  fi
  if [ -z "${GRP_AUTHKIT_CLIENT_ID:-}" ]; then
    echo "FATAL: GRP_OAUTH_ENABLED=1 but GRP_AUTHKIT_CLIENT_ID is unset." >&2
    exit 1
  fi
fi

if [ -z "${LITESTREAM_BUCKET:-}" ]; then
  if [ "${GRP_ALLOW_EPHEMERAL_RECEIPTS:-}" != "1" ]; then
    echo "FATAL: LITESTREAM_BUCKET unset — receipts would not survive a redeploy." >&2
    echo "       Set it, or set GRP_ALLOW_EPHEMERAL_RECEIPTS=1 to accept that." >&2
    exit 1
  fi
  echo "WARNING: replication disabled — receipts die with this container." >&2
  python -c "from app.mcp import store; store.init()"
  exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
fi

# The app derives its own path from settings; asserting they agree stops
# litestream from faithfully replicating a file nothing ever writes to.
APP_DB=$(python -c "from app.mcp import store; print(store._db_path())")
if [ "$APP_DB" != "$DB" ]; then
  echo "FATAL: app writes $APP_DB but replication is configured for $DB." >&2
  exit 1
fi

# -if-replica-exists: the first ever boot has nothing to restore, and that's fine.
# -if-db-not-exists:  never clobber a live DB with an older replica.
litestream restore -if-replica-exists -if-db-not-exists -config /app/deploy/litestream.yml "$DB"

python -c "from app.mcp import store; store.init()"
echo "receipts restored: $(python -c "
import sqlite3,os
print(sqlite3.connect(os.environ['GRP_DB_PATH']).execute('select count(*) from receipts').fetchone()[0])
")"

exec litestream replicate -config /app/deploy/litestream.yml \
  -exec "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"
