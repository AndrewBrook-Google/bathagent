"""CymbalAir demo data. `python3 seed_air.py` = one-shot full restore:
drops and recreates the cymbalair database on BOTH containers, seeds the
primary, clones the sandbox from the primary via pg_dump.
"""
import os
import pathlib
import subprocess
import sys

os.environ["WF_DBNAME"] = "cymbalair"
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "harness"))
import db  # noqa: E402

SCHEMA = (pathlib.Path(__file__).parent / "schema_air.sql").read_text()

SEED = """
INSERT INTO flights VALUES
  (455, 'CA-455', 'SFO', 'MCO', '2026-08-01 09:00-07', 'cancelled'),
  (456, 'CA-456', 'SFO', 'MCO', '2026-08-02 08:30-07', 'scheduled'),
  (789, 'CA-789', 'SFO', 'MCO', '2026-08-01 18:30-07', 'scheduled'),
  (320, 'CA-320', 'SFO', 'MCO', '2026-08-01 20:15-07', 'scheduled');

-- CA-789 tonight: exactly 3 seats left (contention target for the two Wingmen)
INSERT INTO seats VALUES
  (1, 789, '12A', 'economy', 'available', NULL),
  (2, 789, '12B', 'economy', 'available', NULL),
  (3, 789, '14C', 'economy', 'available', NULL),
  (4, 789, '10A', 'economy', 'booked', 'BK-EXIST-001'),
  (5, 320, '21A', 'economy', 'available', NULL),
  (6, 320, '21B', 'economy', 'available', NULL),
  (7, 456, '30A', 'economy', 'available', NULL),
  (8, 456, '30B', 'economy', 'available', NULL),
  (9, 456, '30C', 'economy', 'available', NULL),
  (10, 456, '30D', 'economy', 'available', NULL);

INSERT INTO bookings VALUES
  (
   'BK-EXIST-001', 789, 'Frank Miller', '10A', 189.00, 'confirmed', '2026-07-20 10:00-07'),
  -- old bookings for the admin GDPR scenario
  ('BK-2023-101', 456, 'Grace Hopper',  NULL, 210.00, 'cancelled', '2023-05-11 08:00-07'),
  ('BK-2023-102', 456, 'Alan Kay',      NULL, 195.00, 'cancelled', '2023-09-02 12:00-07'),
  ('BK-2024-201', 456, 'Barbara Liskov',NULL, 205.00, 'cancelled', '2024-03-18 09:30-07'),
  ('BK-2025-301', 456, 'Edsger Dijkstra',NULL, 220.00, 'cancelled', '2025-06-25 14:00-07');
"""


# --- native DB principals (simulated IAM DB authn) ------------------------
# The primary's own grants are the write-path enforcement boundary: the merge
# engine connects AS the merge caller, so PG checks table grants / ownership /
# RLS for real. The sandbox stays untrusted scratch space (agents connect as
# superuser there — its permissions don't matter by design).
#   wf_owner  NOLOGIN  owns all tables (DDL requires membership)
#   wingman   booking agent: read all; write bookings+seats
#   devbot    developer agent: member of wf_owner (DDL) + write all
#   opsbot    analytics agent: read all; write bookings only
#   operator  human console operator: member of wf_owner + write all
PRINCIPALS = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='wf_owner') THEN
    CREATE ROLE wf_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='wingman') THEN
    CREATE ROLE wingman LOGIN PASSWORD 'wf'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='devbot') THEN
    CREATE ROLE devbot LOGIN PASSWORD 'wf'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='opsbot') THEN
    CREATE ROLE opsbot LOGIN PASSWORD 'wf'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='operator') THEN
    CREATE ROLE operator LOGIN PASSWORD 'wf'; END IF;
END $$;
GRANT wf_owner TO devbot, operator;
"""

GRANTS = """
GRANT USAGE ON SCHEMA public TO wingman, devbot, opsbot, operator;
GRANT CREATE ON SCHEMA public TO wf_owner;
ALTER TABLE flights  OWNER TO wf_owner;
ALTER TABLE seats    OWNER TO wf_owner;
ALTER TABLE bookings OWNER TO wf_owner;
GRANT SELECT ON flights, seats, bookings TO wingman, devbot, opsbot, operator;
GRANT INSERT, UPDATE, DELETE ON bookings, seats TO wingman;
GRANT INSERT, UPDATE, DELETE ON flights, seats, bookings TO devbot, operator;
GRANT UPDATE ON bookings TO opsbot;
-- every principal may pass the idempotency gate; nothing else on wf_applied
GRANT SELECT, INSERT ON wf_applied TO wingman, devbot, opsbot, operator;
"""


def _admin(host_port, sql):
    conn = db.connect(f"host=127.0.0.1 port={host_port} dbname=postgres "
                      "user=postgres password=wf", autocommit=True)
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


def resync_sandbox():
    """Clone sandbox from the primary's CURRENT state (the checkout/resync op).
    --no-owner/--no-acl: the sandbox is untrusted scratch space — sessions run
    as superuser there; enforcement happens on the primary at merge time."""
    _admin(55433, "DROP DATABASE IF EXISTS cymbalair WITH (FORCE)")
    _admin(55433, "CREATE DATABASE cymbalair")
    subprocess.run(
        "PGPASSWORD=wf pg_dump -h 127.0.0.1 -p 55432 -U postgres --no-owner --no-acl -d cymbalair"
        " | PGPASSWORD=wf psql -q -h 127.0.0.1 -p 55433 -U postgres -d cymbalair",
        shell=True, check=True, capture_output=True)


def reset_demo():
    """One-shot restore of the whole demo world."""
    _admin(55432, "DROP DATABASE IF EXISTS cymbalair WITH (FORCE)")
    _admin(55432, "CREATE DATABASE cymbalair")
    _admin(55432, PRINCIPALS)          # roles are cluster-level, idempotent
    conn = db.connect(db.PRIMARY_DSN)
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute(SEED)
        cur.execute(GRANTS)            # per-object, re-applied on every reset
    conn.commit()
    conn.close()
    resync_sandbox()


if __name__ == "__main__":
    reset_demo()
    conn = db.connect(db.PRIMARY_DSN)
    print("flights :", db.rows(conn, "SELECT count(*) n FROM flights")[0]["n"])
    print("seats   :", db.rows(conn, "SELECT count(*) n FROM seats")[0]["n"])
    print("bookings:", db.rows(conn, "SELECT count(*) n FROM bookings")[0]["n"])
    conn.close()
    print("demo world restored (primary + sandbox clone)")
