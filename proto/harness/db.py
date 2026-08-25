"""Connection helpers for the Wildfire capture/apply prototype."""
import os
import psycopg2
import psycopg2.extras

DBNAME = os.environ.get("WF_DBNAME", "postgres")
PRIMARY_DSN = os.environ.get("WF_PRIMARY_DSN", f"host=127.0.0.1 port=55432 dbname={DBNAME} user=postgres password=wf")
SANDBOX_DSN = os.environ.get("WF_SANDBOX_DSN", f"host=127.0.0.1 port=55433 dbname={DBNAME} user=postgres password=wf")


def primary_dsn_as(user):
    """Primary connection authenticated as a specific principal (simulated
    IAM DB authn — in the real system the EUC's IAM identity IS the DB user).
    The merge engine uses this so the PRIMARY enforces the caller's own
    grants/ownership/RLS during apply."""
    if "WF_PRIMARY_DSN" in os.environ:
        base = os.environ["WF_PRIMARY_DSN"]
        # Replace user if user is present in DSN
        if "user=" in base:
            import re
            return re.sub(r"user=\S+", f"user={user}", base)
    return f"host=127.0.0.1 port=55432 dbname={DBNAME} user={user} password=wf"


def connect(dsn, autocommit=False):
    conn = psycopg2.connect(dsn)
    conn.autocommit = autocommit
    return conn


def rows(conn, sql, args=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def run(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount
