"""Seed primary with BathStuff data, then clone the state into the sandbox.

The sandbox clone is the 'data isolation' step: a physical copy taken at
capture time. Both databases start identical; drift is injected on the
primary afterwards.
"""
import pathlib
import db

SCHEMA = (pathlib.Path(__file__).parent.parent / "eval" / "schema.sql").read_text()

N_TOOTHBRUSH_ORDERS = 200   # Elbonia toothbrush orders after Jan 1 (the target set)
N_OTHER_ORDERS = 300        # noise rows the agent must not touch


def seed(conn):
    with conn.cursor() as cur:
        # full schema reset: v2 cases create tables / add columns, plain
        # DROP TABLE of the seed tables would leave those behind
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cur.execute(SCHEMA)
        cur.execute("""CREATE TABLE wf_applied(
            action_id text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        cur.execute("""
            INSERT INTO products (id, name, category, imported) VALUES
              (1, 'SparkleBrush 3000', 'toothbrush', true),
              (2, 'PlainBrush',        'toothbrush', false),
              (3, 'MintBlast Paste',   'toothpaste', true)
        """)
        # target set: imported toothbrush orders in Elbonia after Jan 1 2026
        cur.execute("""
            INSERT INTO orders (id, product_id, country, ordered_at, price, tax, total)
            SELECT i, 1, 'Elbonia',
                   '2026-01-01'::timestamptz + (i || ' hours')::interval,
                   20.00, 4.00, 24.00
            FROM generate_series(1, %s) i
        """, (N_TOOTHBRUSH_ORDERS,))
        # noise: other products / countries / pre-2026 orders
        cur.execute("""
            INSERT INTO orders (id, product_id, country, ordered_at, price, tax, total)
            SELECT 100000 + i,
                   CASE WHEN i %% 3 = 0 THEN 2 WHEN i %% 3 = 1 THEN 3 ELSE 1 END,
                   CASE WHEN i %% 2 = 0 THEN 'Freedonia' ELSE 'Elbonia' END,
                   CASE WHEN i %% 3 = 2 THEN '2025-06-01'::timestamptz
                        ELSE '2026-02-01'::timestamptz END,
                   15.00, 3.00, 18.00
            FROM generate_series(1, %s) i
        """, (N_OTHER_ORDERS,))
    conn.commit()


def reset_both():
    """Fresh identical state on primary and sandbox."""
    for dsn in (db.PRIMARY_DSN, db.SANDBOX_DSN):
        conn = db.connect(dsn)
        seed(conn)
        conn.close()


if __name__ == "__main__":
    reset_both()
    conn = db.connect(db.PRIMARY_DSN)
    print("primary orders:", db.rows(conn, "SELECT count(*) AS n FROM orders")[0]["n"])
