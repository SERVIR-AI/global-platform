#!/bin/sh
# Restore the receipt institution, then serve under continuous replication.
#
# FAILS CLOSED on both counts. An unauthenticated public MCP server and a server
# whose receipts silently die on redeploy are the two ways this deployment can be
# quietly wrong, so neither is reachable by forgetting an env var — each needs an
# explicit opt-out you have to type on purpose.
set -eu

DB="${GRP_DB_PATH:-/app/cache/mcp/grp.db}"
mkdir -p "$(dirname "$DB")"

if [ -z "${GRP_API_TOKEN:-}" ] && [ "${GRP_ALLOW_ANONYMOUS:-}" != "1" ]; then
  echo "FATAL: GRP_API_TOKEN unset — the tools would be open to the internet." >&2
  echo "       Set it, or set GRP_ALLOW_ANONYMOUS=1 to serve without a gate." >&2
  exit 1
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
