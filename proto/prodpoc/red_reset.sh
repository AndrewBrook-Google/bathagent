#!/bin/bash
# Rebuild the red-team world: fresh primary from seed, fresh clone, new session.
set -e
PGPASSWORD=wf psql -h 127.0.0.1 -p 55442 -U postgres -q -c "DROP DATABASE IF EXISTS agentt_pri WITH (FORCE)" -c "CREATE DATABASE agentt_pri"
PGPASSWORD=wf psql -h 127.0.0.1 -p 55442 -U postgres -d agentt_pri -q -f /tmp/agentt_seed.sql
PGPASSWORD=wf psql -h 127.0.0.1 -p 55443 -U postgres -q -c "DROP DATABASE IF EXISTS agentt_sbx WITH (FORCE)" -c "CREATE DATABASE agentt_sbx"
PGPASSWORD=wf pg_dump -h 127.0.0.1 -p 55442 -U postgres --no-owner --no-acl -d agentt_pri | PGPASSWORD=wf psql -q -h 127.0.0.1 -p 55443 -U postgres -d agentt_sbx > /dev/null
curl -s -XPOST localhost:55501/v1/session/start -H 'Content-Type: application/json' -d '{"scope":["customers","orders","order_items","inventory"]}'
echo
