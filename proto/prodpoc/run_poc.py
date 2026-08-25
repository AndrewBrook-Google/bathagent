"""Prod-PoC end-to-end: journal capture -> delta -> SQL -> staged apply.

Round 1: pure-DML happy path — FK-ordered inserts/deletes, composite PK,
  pk-changing update, net-zero ops, scratch table, column-disjoint drift.
Round 2: same-column drift               -> data_conflict, full rollback.
Round 3: type change in sandbox          -> capture refused (whitelist).
Round 4: DDL on primary post-capture     -> schema_drift (checkpoint #2).
Round 5: mixed DDL+DML happy path — add column + backfill + SET NOT NULL,
  CREATE TABLE + rows, DROP COLUMN; three-phase apply, drift tolerated.
Round 6: racing CREATE TABLE on primary  -> schema_drift.
Plus: duplicate apply -> idempotency gate.
"""
import subprocess
import uuid

import psycopg2

import wf_capture as cap
import wf_sqlgen as gen

P_PORT, S_PORT, DB = 55442, 55443, "prodpoc"
P_DSN = f"host=127.0.0.1 port={P_PORT} dbname={DB} user=postgres password=wf"
S_DSN = f"host=127.0.0.1 port={S_PORT} dbname={DB} user=postgres password=wf"
SCOPE = ["customers", "orders", "order_items"]

SEED = """
CREATE TABLE customers(id bigint PRIMARY KEY, name text NOT NULL, tier text);
CREATE TABLE orders(id bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    amount numeric(10,2) NOT NULL, note text,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE order_items(order_id bigint REFERENCES orders(id),
    sku text, qty int NOT NULL, PRIMARY KEY (order_id, sku));
INSERT INTO customers VALUES (1,'Ada','gold'), (2,'Bob','silver');
INSERT INTO orders VALUES
  (1,1,100.00,'first', '2026-08-01 10:00Z'),
  (2,1,200.00,'second','2026-08-02 10:00Z'),
  (3,2, 50.00,'third', '2026-08-03 10:00Z'),
  (4,2, 75.00,'fourth','2026-08-04 10:00Z');
INSERT INTO order_items VALUES (1,'SKU-A',2),(1,'SKU-B',1),(4,'SKU-C',3);
"""


def admin(port, sql):
    c = psycopg2.connect(f"host=127.0.0.1 port={port} dbname=postgres "
                         f"user=postgres password=wf")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(sql)
    c.close()


def reset_world():
    for port in (P_PORT, S_PORT):
        admin(port, f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin(P_PORT, f"CREATE DATABASE {DB}")
    c = psycopg2.connect(P_DSN)
    with c.cursor() as cur:
        cur.execute(SEED)
    c.commit()
    c.close()
    clone_sandbox()


def clone_sandbox():
    admin(S_PORT, f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
    admin(S_PORT, f"CREATE DATABASE {DB}")
    subprocess.run(
        f"PGPASSWORD=wf pg_dump -h 127.0.0.1 -p {P_PORT} -U postgres "
        f"--no-owner --no-acl -d {DB} | "
        f"PGPASSWORD=wf psql -q -h 127.0.0.1 -p {S_PORT} -U postgres -d {DB}",
        shell=True, check=True, capture_output=True)


def q(conn, sql):
    return cap.rows(conn, sql)


def section(title):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        raise SystemExit(f"assertion failed: {name}")


# ---------------------------------------------------------------- round 1
def round1():
    section("ROUND 1 — happy path (mixed DML, FK order, drift tolerance)")
    reset_world()
    sbx = psycopg2.connect(S_DSN)
    cap.install(sbx, SCOPE)
    print("  journal triggers installed on", SCOPE)

    # the "agent": a completely ordinary direct connection — no wrapper
    agent = psycopg2.connect(S_DSN)
    with agent.cursor() as c:
        c.execute("INSERT INTO customers VALUES (3,'Cyd','gold')")
        c.execute("INSERT INTO orders VALUES (10,3,300.00,'cyd-1'),"
                  "                          (11,3,120.50,'cyd-2')")
        c.execute("INSERT INTO order_items VALUES (10,'SKU-A',5)")
        c.execute("UPDATE orders SET amount = 110.00 WHERE id = 1")
        c.execute("UPDATE orders SET amount = 115.00 WHERE id = 1")   # 2nd touch
        c.execute("UPDATE orders SET amount = 222.00 WHERE id = 2")   # amount only
        c.execute("UPDATE orders SET id = 999 WHERE id = 3")          # pk change
        c.execute("DELETE FROM order_items WHERE order_id = 4")
        c.execute("DELETE FROM orders WHERE id = 4")
        c.execute("INSERT INTO customers VALUES (9,'Tmp','x')")       # net zero:
        c.execute("DELETE FROM customers WHERE id = 9")               #   I then D
        c.execute("UPDATE customers SET tier = tier WHERE id = 1")    # no-op write
        c.execute("UPDATE order_items SET qty = 9 WHERE order_id=1 AND sku='SKU-B'")
        c.execute("CREATE TABLE scratch AS SELECT * FROM orders")     # out of scope
    agent.commit()

    # concurrent drift on the primary: different COLUMN of a row we touched
    pri = psycopg2.connect(P_DSN)
    with pri.cursor() as c:
        c.execute("UPDATE orders SET note = 'ops touched' WHERE id = 2")
    pri.commit()

    cs = cap.capture(sbx)
    st = cs["stats"]
    print(f"  capture: {st['journal_rows']} journal rows -> "
          f"{st['delta_rows']} net delta rows (net-zero dropped, "
          f"scratch table ignored)")
    for d in sorted(cs["data_delta"], key=lambda d: (d["relid"], str(d["pk"]))):
        print(f"    {d['relid']:<12} {d['op']}  pk={d['pk']}  changed={d['changed'] if d['op']=='U' else '-'}")

    print("\n  -- literal SQL (review rendering) --")
    for stmt in gen.literal_sql(sbx, cs):
        print("   ", stmt)

    action = str(uuid.uuid4())
    res = gen.apply_staged(pri, cs, action)
    print("\n  staged apply:", res)
    check("apply outcome", res.outcome == "applied")

    check("new customer + FK'd orders landed",
          q(pri, "SELECT count(*) n FROM orders WHERE customer_id=3")[0]["n"] == 2)
    check("second touch wins (order 1 amount = 115)",
          str(q(pri, "SELECT amount FROM orders WHERE id=1")[0]["amount"]) == "115.00")
    r2 = q(pri, "SELECT amount, note FROM orders WHERE id=2")[0]
    check("column-disjoint drift preserved (amount=222, note='ops touched')",
          str(r2["amount"]) == "222.00" and r2["note"] == "ops touched")
    check("pk-changing update = D+I (3 gone, 999 present)",
          q(pri, "SELECT count(*) n FROM orders WHERE id IN (3,999)")[0]["n"] == 1
          and q(pri, "SELECT id FROM orders WHERE id=999")[0]["id"] == 999)
    check("FK-ordered deletes (order 4 + its items gone)",
          q(pri, "SELECT count(*) n FROM orders WHERE id=4")[0]["n"] == 0
          and q(pri, "SELECT count(*) n FROM order_items WHERE order_id=4")[0]["n"] == 0)
    check("composite-PK update (item qty=9)",
          q(pri, "SELECT qty FROM order_items WHERE order_id=1 AND sku='SKU-B'")[0]["qty"] == 9)
    check("net-zero rows never traveled (no customer 9)",
          q(pri, "SELECT count(*) n FROM customers WHERE id=9")[0]["n"] == 0)

    res2 = gen.apply_staged(pri, cs, action)
    check("idempotency gate blocks duplicate apply",
          res2.outcome == "error" and "wf_applied" in res2.detail, res2.detail[:60])

    bad = gen.verify_conformance(pri, cs)
    check("delta conformance: every delta row landed exactly", not bad,
          f"{len(cs['data_delta'])} rows checked")
    resid = gen.residual_diff(pri, sbx, SCOPE)
    print("  residual primary<->sandbox diff (must be EXACTLY the tolerated drift):")
    for t, side, row in resid:
        print(f"    {t} {side}: {row[:110]}")
    check("residual == exactly the tolerated drift (order 2 note, both sides)",
          len(resid) == 2 and all(t == "orders" and '"id": 2' in row
                                  for t, _, row in resid))
    sbx.close(); agent.close(); pri.close()


# ---------------------------------------------------------------- round 2
def round2():
    section("ROUND 2 — same-column drift -> data_conflict")
    reset_world()
    sbx = psycopg2.connect(S_DSN)
    cap.install(sbx, SCOPE)
    agent = psycopg2.connect(S_DSN)
    with agent.cursor() as c:
        c.execute("UPDATE orders SET amount = 111.00 WHERE id = 1")
    agent.commit()
    pri = psycopg2.connect(P_DSN)
    with pri.cursor() as c:
        c.execute("UPDATE orders SET amount = 999.99 WHERE id = 1")   # same column!
    pri.commit()
    cs = cap.capture(sbx)
    res = gen.apply_staged(pri, cs, str(uuid.uuid4()))
    print("  apply:", res)
    check("conflict detected, full rollback", res.outcome == "data_conflict")
    check("primary untouched (amount stays 999.99)",
          str(q(pri, "SELECT amount FROM orders WHERE id=1")[0]["amount"]) == "999.99")
    sbx.close(); agent.close(); pri.close()


# ---------------------------------------------------------------- round 3
def round3():
    section("ROUND 3 — type change in sandbox -> capture refused (whitelist)")
    reset_world()
    sbx = psycopg2.connect(S_DSN)
    cap.install(sbx, SCOPE)
    agent = psycopg2.connect(S_DSN)
    with agent.cursor() as c:
        c.execute("ALTER TABLE orders ALTER COLUMN note TYPE varchar(64)")
        c.execute("UPDATE orders SET amount = 105.00 WHERE id = 1")
    agent.commit()
    try:
        cap.capture(sbx)
        check("capture refused", False)
    except cap.SchemaChanged as e:
        check("capture refused with whitelist reason", "type change" in str(e),
              str(e)[:90])
    sbx.close(); agent.close()


# ---------------------------------------------------------------- round 4
def round4():
    section("ROUND 4 — DDL on primary post-capture -> schema_drift (checkpoint #2)")
    reset_world()
    sbx = psycopg2.connect(S_DSN)
    cap.install(sbx, SCOPE)
    agent = psycopg2.connect(S_DSN)
    with agent.cursor() as c:
        c.execute("UPDATE orders SET amount = 130.00 WHERE id = 1")
    agent.commit()
    cs = cap.capture(sbx)
    pri = psycopg2.connect(P_DSN)
    with pri.cursor() as c:
        c.execute("ALTER TABLE orders ADD COLUMN surcharge numeric")  # drift!
    pri.commit()
    res = gen.apply_staged(pri, cs, str(uuid.uuid4()))
    print("  apply:", res)
    check("schema drift detected, full rollback", res.outcome == "schema_drift")
    check("no data applied",
          str(q(pri, "SELECT amount FROM orders WHERE id=1")[0]["amount"]) == "100.00")
    sbx.close(); agent.close(); pri.close()


# ---------------------------------------------------------------- round 5
def round5():
    section("ROUND 5 — mixed DDL+DML: add col + backfill + NOT NULL, "
            "new table, drop col")
    reset_world()
    sbx = psycopg2.connect(S_DSN)
    cap.install(sbx, SCOPE)
    agent = psycopg2.connect(S_DSN)
    with agent.cursor() as c:
        c.execute("ALTER TABLE orders ADD COLUMN loyalty_points integer")
        c.execute("UPDATE orders SET loyalty_points = floor(amount/10)")
        c.execute("ALTER TABLE orders ALTER COLUMN loyalty_points SET NOT NULL")
        c.execute("CREATE TABLE loyalty_tiers(id bigint PRIMARY KEY, "
                  "name text NOT NULL, min_points int NOT NULL)")
        c.execute("INSERT INTO loyalty_tiers VALUES (1,'bronze',0),"
                  "(2,'silver',10),(3,'gold',25)")
        c.execute("ALTER TABLE customers DROP COLUMN tier")
        c.execute("UPDATE orders SET note = 'vip' WHERE id = 1")
    agent.commit()

    pri = psycopg2.connect(P_DSN)
    with pri.cursor() as c:                        # column-disjoint drift
        c.execute("UPDATE orders SET note = 'ops touched' WHERE id = 2")
    pri.commit()

    cs = cap.capture(sbx)
    print(f"  capture: {cs['stats']['ddl_ops']} schema ops, "
          f"{cs['stats']['delta_rows']} data rows")
    for o in cs["schema_delta"]:
        print(f"    DDL  {o['op']:<13} {o['table']}" +
              (f".{o['col']}" if 'col' in o else ""))
    print("\n  -- generated three-phase script --")
    for stmt in gen.literal_sql(sbx, cs):
        print("   ", stmt)

    res = gen.apply_staged(pri, cs, str(uuid.uuid4()))
    print("\n  staged apply:", res)
    check("apply outcome", res.outcome == "applied")
    check("loyalty_points backfilled + NOT NULL",
          q(pri, "SELECT count(*) n FROM orders WHERE loyalty_points IS NULL")[0]["n"] == 0
          and q(pri, """SELECT a.attnotnull nn FROM pg_attribute a
                WHERE a.attrelid='orders'::regclass
                  AND a.attname='loyalty_points'""")[0]["nn"] is True
          and q(pri, "SELECT loyalty_points FROM orders WHERE id=1")[0]["loyalty_points"] == 10)
    check("new table landed with rows",
          q(pri, "SELECT count(*) n FROM loyalty_tiers")[0]["n"] == 3)
    check("dropped column gone on primary",
          q(pri, """SELECT count(*) n FROM information_schema.columns
                WHERE table_name='customers' AND column_name='tier'""")[0]["n"] == 0)
    r2 = q(pri, "SELECT loyalty_points, note FROM orders WHERE id=2")[0]
    check("drift preserved across the DDL (order 2: points=20, note kept)",
          r2["loyalty_points"] == 20 and r2["note"] == "ops touched")

    bad = gen.verify_conformance(pri, cs)
    check("delta conformance across DDL", not bad,
          f"{len(cs['data_delta'])} rows checked")
    resid = gen.residual_diff(pri, sbx, SCOPE + ["loyalty_tiers"])
    print("  residual primary<->sandbox diff:")
    for t, side, row in resid:
        print(f"    {t} {side}: {row[:110]}")
    check("residual == exactly the tolerated drift, even after DDL",
          len(resid) == 2 and all(t == "orders" and '"id": 2' in row
                                  for t, _, row in resid))
    sbx.close(); agent.close(); pri.close()


# ---------------------------------------------------------------- round 6
def round6():
    section("ROUND 6 — table already exists on primary -> schema_drift")
    reset_world()
    sbx = psycopg2.connect(S_DSN)
    cap.install(sbx, SCOPE)
    agent = psycopg2.connect(S_DSN)
    with agent.cursor() as c:
        c.execute("CREATE TABLE loyalty_tiers(id bigint PRIMARY KEY, name text)")
        c.execute("INSERT INTO loyalty_tiers VALUES (1,'bronze')")
    agent.commit()
    cs = cap.capture(sbx)
    pri = psycopg2.connect(P_DSN)
    with pri.cursor() as c:                        # racing creation on primary
        c.execute("CREATE TABLE loyalty_tiers(id bigint PRIMARY KEY, name text)")
    pri.commit()
    res = gen.apply_staged(pri, cs, str(uuid.uuid4()))
    print("  apply:", res)
    check("racing create detected", res.outcome == "schema_drift"
          and "already exists" in res.detail, res.detail[:60])
    sbx.close(); agent.close(); pri.close()


if __name__ == "__main__":
    round1()
    round2()
    round3()
    round4()
    round5()
    round6()
    print("\nALL ROUNDS PASSED")
