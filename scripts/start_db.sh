#!/usr/bin/env bash
# Start the local PostgreSQL (extracted from Debian debs, no root needed).
set -euo pipefail

PGBIN=/workspace/pgsql/usr/lib/postgresql/15/bin
PGLIB=/workspace/pgsql/usr/lib/aarch64-linux-gnu
PGDATA=/workspace/pgdata
PGLOG=/workspace/pgdata.log
PORT=5432

export LD_LIBRARY_PATH="$PGLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$PGBIN:$PATH"

if [ ! -d "$PGDATA/base" ]; then
  echo "initdb..."
  initdb -D "$PGDATA" -U postgres --auth=trust
fi

if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  pg_ctl -D "$PGDATA" -l "$PGLOG" -o "-p $PORT -k /tmp -c listen_addresses=127.0.0.1" start
  sleep 2
fi

for db in annotation annotation_test; do
  psql -h 127.0.0.1 -p $PORT -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
    || psql -h 127.0.0.1 -p $PORT -U postgres -c "CREATE DATABASE $db"
done
psql -h 127.0.0.1 -p $PORT -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='annotator'" | grep -q 1 \
  || psql -h 127.0.0.1 -p $PORT -U postgres -c "CREATE USER annotator WITH PASSWORD 'annotator' SUPERUSER"

echo "PostgreSQL ready on 127.0.0.1:$PORT (dbs: annotation, annotation_test)"
