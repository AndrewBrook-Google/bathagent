"""Extensive test matrix for the state-diff capture/apply engine.

Groups:
  T  type fidelity through capture->apply (the jsonb round-trip risk)
  M  DML netting semantics (first-touch journal algebra)
  S  schema-delta whitelist: supported ops + every refusal
  C  concurrency / drift / constraint-ordering
  L  lifecycle (reject->fixup->recapture, double install, rollback residue)
  P  scale smoke (timings only)
  R  adversarial round 1: kills found by a sub-agent operating the DB directly
  G  server-written columns (identity / GENERATED STORED / collation)
  H  adversarial round 2: values the row model cannot represent, objects the
     catalog model cannot see, and DDL that hides outside both

Every positive case ends with BOTH verifiers:
  conformance — every delta row landed exactly on the primary
  residual    — full-table primary<->sandbox diff == expected drift (usually [])
"""
import json
import subprocess
import time
import traceback
import uuid

import psycopg2

import wf_capture as cap
import wf_sqlgen as gen

PP, SP = 55442, 55443


def admin(port, sql):
    c = psycopg2.connect(f"host=127.0.0.1 port={port} dbname=postgres "
                         f"user=postgres password=wf")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(sql)
    c.close()


def dsn(port, db):
    return f"host=127.0.0.1 port={port} dbname={db} user=postgres password=wf"


class W:
    """One fresh world per case: primary mtx_p + n sandbox clones mtx_s<i>."""

    def __init__(self, seed, scope, nsbx=1):
        admin(PP, "DROP DATABASE IF EXISTS mtx_p WITH (FORCE)")
        admin(PP, "CREATE DATABASE mtx_p")
        self.pri = psycopg2.connect(dsn(PP, "mtx_p"))
        with self.pri.cursor() as cur:
            cur.execute(seed)
        self.pri.commit()
        self.scope, self.sbx = scope, []
        for i in range(nsbx):
            db = f"mtx_s{i}"
            admin(SP, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
            admin(SP, f"CREATE DATABASE {db}")
            subprocess.run(
                f"PGPASSWORD=wf pg_dump -h 127.0.0.1 -p {PP} -U postgres "
                f"--no-owner --no-acl -d mtx_p | "
                f"PGPASSWORD=wf psql -q -h 127.0.0.1 -p {SP} -U postgres -d {db}",
                shell=True, check=True, capture_output=True)
            c = psycopg2.connect(dsn(SP, db))
            cap.install(c, scope)
            self.sbx.append(c)

    def agent(self, i=0):
        """A plain, direct connection — exactly what a real agent gets."""
        return psycopg2.connect(dsn(SP, f"mtx_s{i}"))

    def go(self, i, *stmts):
        a = self.agent(i)
        with a.cursor() as c:
            for s in stmts:
                c.execute(s)
        a.commit()
        a.close()

    def apply(self, cs):
        return gen.apply_staged(self.pri, cs, str(uuid.uuid4()))

    def close(self):
        self.pri.close()
        for c in self.sbx:
            c.close()


def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)


def clean(w, cs, i=0, tables=None, expect_residual=0):
    bad = gen.verify_conformance(w.pri, cs)
    ok(not bad, f"conformance violations: {bad}")
    resid = gen.residual_diff(w.pri, w.sbx[i], tables or w.scope)
    ok(len(resid) == expect_residual,
       f"residual expected {expect_residual}, got {len(resid)}: {resid[:4]}")


def find(cs, table, **pk):
    return [d for d in cs["data_delta"]
            if d["relid"] == table and all(d["pk"].get(k) == v
                                           for k, v in pk.items())]


SEED_STD = """
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
SCOPE_STD = ["customers", "orders", "order_items"]

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ============================================================ T: type fidelity
def _typeworld(cols, values_sql, scope=("typ",)):
    return W(f"CREATE TABLE typ(id bigint PRIMARY KEY, {cols});"
             f"INSERT INTO typ VALUES {values_sql};", list(scope))


@case("T01 numeric precision + negatives")
def t01():
    w = _typeworld("a numeric(12,4), b numeric",
                   "(1, 0.0001, 12345678901234567890.123456789),"
                   "(2, -9999999.9999, 0)")
    w.go(0, "UPDATE typ SET a = -0.0001 WHERE id=1",
            "INSERT INTO typ VALUES (3, 99999999.9999, 1e30)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T02 int boundaries (bigint min/max, smallint)")
def t02():
    w = _typeworld("big bigint, small smallint",
                   "(1, 9223372036854775807, 32767), (2, -9223372036854775808, -32768)")
    w.go(0, "UPDATE typ SET big = big - 1 WHERE id=1",
            "INSERT INTO typ VALUES (3, -9223372036854775808, 0)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T03 float8 representations")
def t03():
    w = _typeworld("f float8", "(1, 3.141592653589793), (2, 1e-300), (3, -1e308)")
    w.go(0, "UPDATE typ SET f = 2.718281828459045 WHERE id=1",
            "INSERT INTO typ VALUES (4, 5e-324)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T04 text edge cases (quotes, unicode, newline, empty vs NULL)")
def t04():
    w = _typeworld("s text", "(1, 'plain'), (2, NULL)")
    a = w.agent(0)
    with a.cursor() as c:
        c.execute("UPDATE typ SET s = %s WHERE id=1",
                  ("O'Brien \"quoted\" \\back\\ 中文 🚀 line1\nline2\ttab",))
        c.execute("INSERT INTO typ VALUES (3, %s)", ("",))   # empty ≠ NULL
        c.execute("UPDATE typ SET s = %s WHERE id=2", ("was null",))
    a.commit(); a.close()
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    r = cap.rows(w.pri, "SELECT s FROM typ WHERE id=3")[0]["s"]
    ok(r == "", "empty string survived as empty, not NULL")
    w.close()


@case("T05 timestamptz microseconds / date / time")
def t05():
    w = _typeworld("ts timestamptz, d date, tm time",
                   "(1, '2026-01-02 03:04:05.123456+07', '2000-02-29', '23:59:59.999999')")
    w.go(0, "UPDATE typ SET ts = '1999-12-31 23:59:59.000001Z' WHERE id=1",
            "INSERT INTO typ VALUES (2, '0001-01-01 00:00:00Z', '0001-01-01', '00:00')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T06 uuid + boolean")
def t06():
    w = _typeworld("u uuid, b boolean",
                   "(1, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', true)")
    w.go(0, "UPDATE typ SET b = false WHERE id=1",
            "INSERT INTO typ VALUES (2, 'ffffffff-ffff-ffff-ffff-ffffffffffff', NULL)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T07 bytea round-trip")
def t07():
    w = _typeworld("blob bytea", r"(1, E'\\xDEADBEEF')")
    w.go(0, r"UPDATE typ SET blob = E'\\x00FF10' WHERE id=1",
            r"INSERT INTO typ VALUES (2, E'\\x')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T08 jsonb column (nested)")
def t08():
    w = _typeworld("j jsonb", """(1, '{"a":{"b":[1,2,3]},"s":"x"}')""")
    w.go(0, """UPDATE typ SET j = jsonb_set(j,'{a,b,1}','99') WHERE id=1""",
            """INSERT INTO typ VALUES (2, '[{"k":null},"str",42]')""")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T09 arrays (int[], text[])")
def t09():
    w = _typeworld("ia int[], ta text[]",
                   "(1, '{1,2,3}', '{\"a\",\"b\"}')")
    w.go(0, "UPDATE typ SET ia = ia || 4, ta = array_append(ta,'c,c') WHERE id=1",
            "INSERT INTO typ VALUES (2, '{}', ARRAY['x''y','z\"w'])")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T10 char(n) padding + varchar limit")
def t10():
    w = _typeworld("c3 char(3), v5 varchar(5)", "(1, 'ab', 'exact')")
    w.go(0, "UPDATE typ SET c3 = 'x' WHERE id=1",
            "INSERT INTO typ VALUES (2, 'abc', '12345')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("T11 NULL transitions both directions")
def t11():
    w = _typeworld("x int, y int", "(1, 10, NULL), (2, NULL, 20)")
    w.go(0, "UPDATE typ SET x = NULL, y = 1 WHERE id=1",
            "UPDATE typ SET x = 5, y = NULL WHERE id=2")
    cs = cap.capture(w.sbx[0])
    d = find(cs, "typ", id=1)[0]
    ok(sorted(d["changed"]) == ["x", "y"], f"changed={d['changed']}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


# ============================================================ M: DML netting
@case("M01 insert then update -> single I with final values")
def m01():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "INSERT INTO customers VALUES (5,'Eve','new')",
            "UPDATE customers SET tier='vip' WHERE id=5")
    cs = cap.capture(w.sbx[0])
    d = find(cs, "customers", id=5)
    ok(len(d) == 1 and d[0]["op"] == "I" and d[0]["after"]["tier"] == "vip",
       f"expected single I with vip, got {d}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M02 update then delete -> single D with ORIGINAL before-image")
def m02():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items WHERE order_id=4",
            "UPDATE orders SET amount=999 WHERE id=4",
            "DELETE FROM orders WHERE id=4")
    cs = cap.capture(w.sbx[0])
    d = find(cs, "orders", id=4)
    ok(len(d) == 1 and d[0]["op"] == "D"
       and float(d[0]["before"]["amount"]) == 75.0,
       f"D must carry first-touch before (75.00), got {d}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M03 delete then re-insert different values -> net U")
def m03():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items WHERE order_id=1 AND sku='SKU-B'",
            "INSERT INTO order_items VALUES (1,'SKU-B',42)")
    cs = cap.capture(w.sbx[0])
    d = find(cs, "order_items", sku="SKU-B")
    ok(len(d) == 1 and d[0]["op"] == "U" and d[0]["changed"] == ["qty"],
       f"expected net U on qty, got {d}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M04 delete then re-insert SAME values -> net zero")
def m04():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items WHERE order_id=1 AND sku='SKU-B'",
            "INSERT INTO order_items VALUES (1,'SKU-B',1)")
    cs = cap.capture(w.sbx[0])
    ok(not cs["data_delta"], f"expected empty delta, got {cs['data_delta']}")
    w.close()


@case("M05 100 updates of one row -> 1 journal row, 1 delta row")
def m05():
    w = W(SEED_STD, SCOPE_STD)
    a = w.agent(0)
    with a.cursor() as c:
        for i in range(100):
            c.execute("UPDATE orders SET amount = %s WHERE id=1", (100 + i,))
    a.commit(); a.close()
    cs = cap.capture(w.sbx[0])
    ok(cs["stats"]["journal_rows"] == 1 and cs["stats"]["delta_rows"] == 1,
       f"stats={cs['stats']}")
    ok(float(find(cs, 'orders', id=1)[0]["after"]["amount"]) == 199.0, "last wins")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M06 whole-table multi-row statement")
def m06():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "UPDATE orders SET note = upper(note)")
    cs = cap.capture(w.sbx[0])
    ok(cs["stats"]["delta_rows"] == 4, f"stats={cs['stats']}")
    ok(all(d["changed"] == ["note"] for d in cs["data_delta"]), "note only")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M07 pk-change chain 3->300->301 -> D(3)+I(301), no trace of 300")
def m07():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "UPDATE orders SET id=300 WHERE id=3",
            "UPDATE orders SET id=301 WHERE id=300")
    cs = cap.capture(w.sbx[0])
    ops = {d["pk"]["id"]: d["op"] for d in cs["data_delta"] if d["relid"] == "orders"}
    ok(ops == {3: "D", 301: "I"}, f"got {ops}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M08 changed-column minimality on no-op assignments")
def m08():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "UPDATE orders SET amount = amount, note = 'poked' WHERE id=1")
    cs = cap.capture(w.sbx[0])
    d = find(cs, "orders", id=1)[0]
    ok(d["changed"] == ["note"], f"changed={d['changed']}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("M09 delete ALL rows of a table + partial re-insert")
def m09():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items",
            "INSERT INTO order_items VALUES (1,'SKU-A',7)")
    cs = cap.capture(w.sbx[0])
    ops = sorted((d["op"]) for d in cs["data_delta"] if d["relid"] == "order_items")
    ok(ops == ["D", "D", "U"], f"got {ops}")   # SKU-A nets to U (qty 2->7)
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


# ============================================================ S: schema delta
@case("S01 add_column constant default, NO backfill — implicit fill matches")
def s01():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN flags int DEFAULT 5")
    cs = cap.capture(w.sbx[0])
    ops = [o["op"] for o in cs["schema_delta"]]
    # engine may append a set_default to textual-align the catalog default
    ok(ops in (["add_column"], ["add_column", "set_default"]), f"ops={ops}")
    ok(not cs["data_delta"], "no data rows needed")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("S02 add_column volatile default -> capture refused")
def s02():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN created2 timestamptz DEFAULT now()")
    try:
        cap.capture(w.sbx[0])
        ok(False, "should have refused")
    except cap.SchemaChanged as e:
        ok("non-constant default" in str(e), str(e)[:80])
    w.close()


@case("S03 add + backfill + SET NOT NULL (loyalty pattern)")
def s03():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN pts int",
            "UPDATE orders SET pts = floor(amount/10)",
            "ALTER TABLE orders ALTER COLUMN pts SET NOT NULL")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("S04 add column then drop it -> net-zero schema delta")
def s04():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN tmp int",
            "UPDATE orders SET tmp = 1 WHERE id = 1",
            "ALTER TABLE orders DROP COLUMN tmp")
    cs = cap.capture(w.sbx[0])
    ok(not cs["schema_delta"], f"schema ops: {cs['schema_delta']}")
    ok(not cs["data_delta"], f"data rows: {cs['data_delta']}")
    w.close()


@case("S05 drop_column with data")
def s05():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers DROP COLUMN tier")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    n = cap.rows(w.pri, """SELECT count(*) n FROM information_schema.columns
        WHERE table_name='customers' AND column_name='tier'""")[0]["n"]
    ok(n == 0, "column gone on primary")
    clean(w, cs); w.close()


@case("S06 create_table (composite pk) + rows; plus an EMPTY new table")
def s06():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE TABLE tag_map(entity text, tag text, w float8 NOT NULL, "
            "PRIMARY KEY (entity, tag))",
            "INSERT INTO tag_map VALUES ('o1','hot',0.9),('o1','new',0.1)",
            "CREATE TABLE empty_t(id bigint PRIMARY KEY, v text)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    ok(cap.rows(w.pri, "SELECT count(*) n FROM tag_map")[0]["n"] == 2, "rows")
    ok(cap.rows(w.pri, "SELECT count(*) n FROM empty_t")[0]["n"] == 0, "empty")
    clean(w, cs, tables=SCOPE_STD + ["tag_map", "empty_t"]); w.close()


@case("S07 create + drop table in one session -> net zero")
def s07():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE TABLE flash(id int PRIMARY KEY)",
            "INSERT INTO flash VALUES (1)",
            "DROP TABLE flash")
    cs = cap.capture(w.sbx[0])
    ok(not cs["schema_delta"] and not cs["data_delta"], "net zero")
    w.close()


@case("S08 drop_table destroys rows on primary (intended)")
def s08():
    w = W(SEED_STD + "CREATE TABLE legacy(id int PRIMARY KEY, v text);"
                     "INSERT INTO legacy VALUES (1,'x');",
          SCOPE_STD + ["legacy"])
    w.go(0, "DROP TABLE legacy")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    ok(cap.rows(w.pri, "SELECT to_regclass('legacy') r")[0]["r"] is None, "gone")
    clean(w, cs, tables=SCOPE_STD); w.close()


@case("S09 RENAME COLUMN -> refused (rows not rewritten, data would be lost)")
def s09():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders RENAME COLUMN note TO memo")
    try:
        cap.capture(w.sbx[0])
        ok(False, "should have refused")
    except cap.SchemaChanged as e:
        ok("rename" in str(e), str(e)[:90])
    w.close()


@case("S10 drop colA + add colB + FULL backfill -> sanctioned rename path")
def s10():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN memo text",
            "UPDATE orders SET memo = note",
            "ALTER TABLE orders DROP COLUMN note")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    r = cap.rows(w.pri, "SELECT memo FROM orders WHERE id=1")[0]["memo"]
    ok(r == "first", "values traveled into the new column")
    clean(w, cs); w.close()


@case("S11 set_default / drop_default (constant)")
def s11():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers ALTER COLUMN tier SET DEFAULT 'basic'")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    d = cap.rows(w.pri, """SELECT column_default d FROM information_schema.columns
        WHERE table_name='customers' AND column_name='tier'""")[0]["d"]
    ok(d and "basic" in d, f"default on primary: {d}")
    clean(w, cs); w.close()


@case("S12 drop_not_null + write a NULL")
def s12():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers ALTER COLUMN name DROP NOT NULL",
            "UPDATE customers SET name = NULL WHERE id = 2")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs); w.close()


@case("S13a refusal: ALTER TYPE")
def s13a():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ALTER COLUMN note TYPE varchar(40)")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("type change" in str(e), str(e)[:80])
    w.close()


@case("S13b refusal: UNIQUE constraint on existing table")
def s13b():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers ADD CONSTRAINT uq UNIQUE (name)")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("constraint/index" in str(e), str(e)[:80])
    w.close()


@case("S13c refusal: CREATE INDEX on existing table")
def s13c():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE INDEX idx_amount ON orders(amount)")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("constraint/index" in str(e), str(e)[:80])
    w.close()


@case("S13d refusal: ADD FOREIGN KEY on existing table")
def s13d():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE order_items ADD CONSTRAINT fk2 "
            "FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("constraint/index" in str(e), str(e)[:80])
    w.close()


@case("S14 refusal: new table with extra index / CHECK")
def s14():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE TABLE nt(id int PRIMARY KEY, v int CHECK (v > 0))")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("non-PK constraints" in str(e), str(e)[:80])
    w.close()


@case("S15 TRUNCATE blocked in the sandbox")
def s15():
    w = W(SEED_STD, SCOPE_STD)
    a = w.agent(0)
    try:
        with a.cursor() as c:
            c.execute("TRUNCATE order_items")
        ok(False, "truncate went through")
    except psycopg2.errors.RaiseException as e:
        ok("not capturable" in str(e), str(e)[:80])
    a.close(); w.close()


@case("S16 add col w/ default + touch same table + change default (agent-found)")
def s16():
    # Found by the autonomous-agent round: implicit backfill value is the
    # ADD-time default, which the final catalog no longer knows once the
    # default is changed. Engine must reconstruct it from untouched rows.
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN wh text DEFAULT 'MAIN'",
            "UPDATE orders SET wh = 'COLD' WHERE id = 3",   # touch same table
            "ALTER TABLE orders ALTER COLUMN wh SET DEFAULT 'ALT'",
            "INSERT INTO orders (id, customer_id, amount) VALUES (50, 1, 9.99)")
    cs = cap.capture(w.sbx[0])
    ops = [(o["op"], o.get("col")) for o in cs["schema_delta"]]
    ok(("set_default", "wh") in ops, f"synthesized set_default missing: {ops}")
    add = [o for o in cs["schema_delta"] if o["op"] == "add_column"][0]
    ok("MAIN" in (add["default"] or ""), f"ADD must backfill MAIN: {add}")
    ok(w.apply(cs).outcome == "applied", "apply")
    r = cap.rows(w.pri, "SELECT wh FROM orders WHERE id = 1")[0]["wh"]
    ok(r == "MAIN", f"untouched row got ADD-time default, got {r}")
    d = cap.rows(w.pri, """SELECT column_default d FROM information_schema.columns
        WHERE table_name='orders' AND column_name='wh'""")[0]["d"]
    ok("ALT" in d, f"final catalog default is ALT: {d}")
    clean(w, cs); w.close()


@case("S17 add col nullable, set default later, no backfill")
def s17():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ADD COLUMN badge text",
            "ALTER TABLE orders ALTER COLUMN badge SET DEFAULT 'x'")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    ok(cap.rows(w.pri, "SELECT count(*) n FROM orders WHERE badge IS NOT NULL")[0]["n"] == 0,
       "untouched rows stay NULL (ADD-time no default)")
    clean(w, cs); w.close()


@case("S18 backfill-evidence exhaustion attack (agent thesis 1)")
def s18():
    # add col DEFAULT B1 -> update EVERY row (destroys untouched-row evidence)
    # -> change default to B3. Journal before-images still testify to B1.
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE order_items ADD COLUMN batch text DEFAULT 'B1'",
            "UPDATE order_items SET batch = 'B2'",           # all rows
            "ALTER TABLE order_items ALTER COLUMN batch SET DEFAULT 'B3'")
    cs = cap.capture(w.sbx[0])
    add = [o for o in cs["schema_delta"] if o["op"] == "add_column"][0]
    ok("B1" in (add["default"] or ""), f"ADD must recover B1: {add['default']}")
    ok(w.apply(cs).outcome == "applied", "apply survives evidence exhaustion")
    d = cap.rows(w.pri, """SELECT column_default d FROM information_schema.columns
        WHERE table_name='order_items' AND column_name='batch'""")[0]["d"]
    ok("B3" in d, f"catalog default B3: {d}")
    clean(w, cs); w.close()


@case("S19 pk graveyard reuse: DELETE k then move another row to k")
def s19():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items WHERE order_id=4",
            "DELETE FROM orders WHERE id=4",
            "UPDATE orders SET id=4 WHERE id=3")   # row 3 moves into 4's grave
    cs = cap.capture(w.sbx[0])
    ops = {d["pk"]["id"]: d["op"] for d in cs["data_delta"] if d["relid"] == "orders"}
    ok(ops.get(3) == "D" and ops.get(4) == "U", f"expected D(3)+U(4): {ops}")
    ok(w.apply(cs).outcome == "applied", "apply")
    r = cap.rows(w.pri, "SELECT amount FROM orders WHERE id=4")[0]
    ok(float(r["amount"]) == 50.0, "grave key now holds the moved row")
    clean(w, cs); w.close()


# ==================================================== C: drift & constraints
@case("C01 unrelated DDL on primary tolerated (per-table locality)")
def c01():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "UPDATE orders SET amount = 101 WHERE id = 1")
    with w.pri.cursor() as c:
        c.execute("CREATE TABLE unrelated(x int PRIMARY KEY)")
        c.execute("ALTER TABLE unrelated ADD COLUMN y int")
    w.pri.commit()
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply despite unrelated DDL")
    clean(w, cs, tables=SCOPE_STD); w.close()


@case("C02 primary deleted a row the sandbox UPDATEd -> conflict")
def c02():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "UPDATE orders SET amount = 101 WHERE id = 1")
    with w.pri.cursor() as c:
        c.execute("DELETE FROM order_items WHERE order_id=1")
        c.execute("DELETE FROM orders WHERE id = 1")
    w.pri.commit()
    r = w.apply(cap.capture(w.sbx[0]))
    ok(r.outcome == "data_conflict", f"got {r}")
    w.close()


@case("C03 primary deleted a row the sandbox DELETEd -> strict conflict")
def c03():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items WHERE order_id=4",
            "DELETE FROM orders WHERE id = 4")
    with w.pri.cursor() as c:
        c.execute("DELETE FROM order_items WHERE order_id=4")
        c.execute("DELETE FROM orders WHERE id = 4")
    w.pri.commit()
    r = w.apply(cap.capture(w.sbx[0]))
    ok(r.outcome == "data_conflict", f"strict OCC treats it as drift: {r}")
    w.close()


@case("C04 primary inserted the same pk the sandbox inserts -> conflict")
def c04():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "INSERT INTO customers VALUES (7,'New','x')")
    with w.pri.cursor() as c:
        c.execute("INSERT INTO customers VALUES (7,'Racer','y')")
    w.pri.commit()
    r = w.apply(cap.capture(w.sbx[0]))
    ok(r.outcome == "data_conflict", f"got {r}")
    w.close()


@case("C05 drift on a column of a row the sandbox DELETEs -> conflict")
def c05():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items WHERE order_id=4",
            "DELETE FROM orders WHERE id=4")
    with w.pri.cursor() as c:
        c.execute("UPDATE orders SET note='changed meanwhile' WHERE id=4")
    w.pri.commit()
    r = w.apply(cap.capture(w.sbx[0]))
    ok(r.outcome == "data_conflict", f"got {r}")
    w.close()


@case("C06 two sandboxes, disjoint rows -> both merge")
def c06():
    w = W(SEED_STD, SCOPE_STD, nsbx=2)
    w.go(0, "UPDATE orders SET amount=110 WHERE id=1")
    w.go(1, "UPDATE orders SET amount=210 WHERE id=2")
    ok(w.apply(cap.capture(w.sbx[0])).outcome == "applied", "cs1")
    ok(w.apply(cap.capture(w.sbx[1])).outcome == "applied", "cs2")
    r = cap.rows(w.pri, "SELECT id, amount FROM orders WHERE id IN (1,2) ORDER BY id")
    ok([float(x["amount"]) for x in r] == [110.0, 210.0], f"{r}")
    w.close()


@case("C07 two sandboxes, same row same column -> second conflicts")
def c07():
    w = W(SEED_STD, SCOPE_STD, nsbx=2)
    w.go(0, "UPDATE orders SET amount=110 WHERE id=1")
    w.go(1, "UPDATE orders SET amount=120 WHERE id=1")
    ok(w.apply(cap.capture(w.sbx[0])).outcome == "applied", "first")
    r = w.apply(cap.capture(w.sbx[1]))
    ok(r.outcome == "data_conflict", f"second must conflict: {r}")
    w.close()


@case("C08 two sandboxes, same row different columns -> both merge")
def c08():
    w = W(SEED_STD, SCOPE_STD, nsbx=2)
    w.go(0, "UPDATE orders SET amount=110 WHERE id=1")
    w.go(1, "UPDATE orders SET note='relabeled' WHERE id=1")
    ok(w.apply(cap.capture(w.sbx[0])).outcome == "applied", "cs1")
    ok(w.apply(cap.capture(w.sbx[1])).outcome == "applied", "cs2 column-merge")
    r = cap.rows(w.pri, "SELECT amount, note FROM orders WHERE id=1")[0]
    ok(float(r["amount"]) == 110.0 and r["note"] == "relabeled",
       f"both edits present: {r}")
    w.close()


@case("C09 self-referential FK insert set in ONE statement -> applies")
def c09():
    w = W("CREATE TABLE emp(id int PRIMARY KEY, boss int REFERENCES emp(id));"
          "INSERT INTO emp VALUES (1, NULL);", ["emp"])
    w.go(0, "INSERT INTO emp VALUES (10, 1), (11, 10), (12, 11)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied",
       "end-of-statement FK check admits chains inside one table")
    clean(w, cs); w.close()


@case("C10 unique-value swap, NON-deferrable -> clean 'error' rollback")
def c10():
    w = W("CREATE TABLE u(id int PRIMARY KEY, email text UNIQUE);"
          "INSERT INTO u VALUES (1,'a@x'), (2,'b@x');", ["u"])
    w.go(0, "UPDATE u SET email='tmp' WHERE id=1",     # agent swaps via temp
            "UPDATE u SET email='a@x' WHERE id=2",
            "UPDATE u SET email='b@x' WHERE id=1")
    r = w.apply(cap.capture(w.sbx[0]))                 # apply = ONE statement
    ok(r.outcome == "error" and "unique" in r.detail.lower(),
       f"per-row unique check must fail the swap cleanly: {r}")
    r2 = [x["email"] for x in cap.rows(w.pri, "SELECT email FROM u ORDER BY id")]
    ok(r2 == ["a@x", "b@x"], f"full rollback, primary intact: {r2}")
    w.close()


@case("C11 unique-value swap, DEFERRABLE -> applies (SET CONSTRAINTS)")
def c11():
    w = W("CREATE TABLE u(id int PRIMARY KEY, email text, "
          "CONSTRAINT u_email UNIQUE (email) DEFERRABLE);"
          "INSERT INTO u VALUES (1,'a@x'), (2,'b@x');", ["u"])
    w.go(0, "UPDATE u SET email='tmp' WHERE id=1",
            "UPDATE u SET email='a@x' WHERE id=2",
            "UPDATE u SET email='b@x' WHERE id=1")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "deferrable swap merges")
    clean(w, cs); w.close()


@case("C12 3-level FK chain inserts, correct topo order")
def c12():
    w = W("CREATE TABLE a(id int PRIMARY KEY);"
          "CREATE TABLE b(id int PRIMARY KEY, a_id int NOT NULL REFERENCES a(id));"
          "CREATE TABLE c(id int PRIMARY KEY, b_id int NOT NULL REFERENCES b(id));",
          ["a", "b", "c"])
    w.go(0, "INSERT INTO a VALUES (1)",
            "INSERT INTO b VALUES (1,1)",
            "INSERT INTO c VALUES (1,1)")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "parent-first insert chain")
    clean(w, cs); w.close()


@case("C13 3-level FK chain deletes, reverse topo order")
def c13():
    w = W("CREATE TABLE a(id int PRIMARY KEY);"
          "CREATE TABLE b(id int PRIMARY KEY, a_id int NOT NULL REFERENCES a(id));"
          "CREATE TABLE c(id int PRIMARY KEY, b_id int NOT NULL REFERENCES b(id));"
          "INSERT INTO a VALUES (1); INSERT INTO b VALUES (1,1);"
          "INSERT INTO c VALUES (1,1);",
          ["a", "b", "c"])
    w.go(0, "DELETE FROM c", "DELETE FROM b", "DELETE FROM a")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "child-first delete chain")
    clean(w, cs); w.close()


# ============================================================ L: lifecycle
@case("L01 reject -> fix up -> recapture: cumulative delta vs same basis")
def l01():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "UPDATE orders SET amount=110 WHERE id=1")
    cs1 = cap.capture(w.sbx[0])            # pretend this got rejected
    ok(cs1["stats"]["delta_rows"] == 1, "first capture")
    w.go(0, "UPDATE orders SET amount=111 WHERE id=1",
            "UPDATE orders SET note='second wave' WHERE id=2")
    cs2 = cap.capture(w.sbx[0])            # capture is a pure read: repeatable
    d1 = find(cs2, "orders", id=1)[0]
    ok(float(d1["after"]["amount"]) == 111.0
       and float(d1["before"]["amount"]) == 100.0,
       "before stays the BASIS value across waves")
    ok(w.apply(cs2).outcome == "applied", "apply cumulative")
    clean(w, cs2); w.close()


@case("L02 double install on one clone -> refused")
def l02():
    w = W(SEED_STD, SCOPE_STD)
    try:
        cap.install(w.sbx[0], SCOPE_STD)
        ok(False, "second install must fail")
    except RuntimeError as e:
        ok("already installed" in str(e), str(e)[:60])
    w.close()


@case("L03 conflict in LAST table rolls back everything (zero residue)")
def l03():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "INSERT INTO customers VALUES (8,'Zed','x')",   # would succeed
            "UPDATE orders SET amount=123 WHERE id=1")       # will conflict
    with w.pri.cursor() as c:
        c.execute("UPDATE orders SET amount=777 WHERE id=1")
    w.pri.commit()
    r = w.apply(cap.capture(w.sbx[0]))
    ok(r.outcome == "data_conflict", f"got {r}")
    n = cap.rows(w.pri, "SELECT count(*) n FROM customers WHERE id=8")[0]["n"]
    ok(n == 0, "customer insert rolled back with the conflict")
    w.close()


@case("L04 empty changeset (agent did nothing) -> empty deltas")
def l04():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "SELECT count(*) FROM orders")
    cs = cap.capture(w.sbx[0])
    ok(not cs["data_delta"] and not cs["schema_delta"], "empty")
    w.close()


# ============================================================ P: scale smoke
@case("P01 30k rows / 5k updates / 1k inserts / 500 deletes — timings")
def p01():
    w = W("CREATE TABLE big(id bigint PRIMARY KEY, v numeric(12,2), s text);"
          "INSERT INTO big SELECT g, g * 1.5, 'row-' || g "
          "FROM generate_series(1, 30000) g;", ["big"])
    a = w.agent(0)
    t0 = time.monotonic()
    with a.cursor() as c:
        c.execute("UPDATE big SET v = v + 1 WHERE id % 6 = 0")           # 5000
        c.execute("INSERT INTO big SELECT 100000 + g, g, 'new-' || g "
                  "FROM generate_series(1, 1000) g")                     # 1000
        c.execute("DELETE FROM big WHERE id % 60 = 1")                   # 500
    a.commit(); a.close()
    t_work = time.monotonic() - t0
    t0 = time.monotonic()
    cs = cap.capture(w.sbx[0])
    t_cap = time.monotonic() - t0
    st = cs["stats"]
    t0 = time.monotonic()
    r = w.apply(cs)
    t_apply = time.monotonic() - t0
    ok(r.outcome == "applied", f"{r}")
    # 17 inserted rows (100000+g, g≡21 mod 60) are hit by the later DELETE:
    # insert-then-delete nets to zero — journal keeps them, delta drops them
    ok(st["journal_rows"] == 6500 and st["delta_rows"] == 6483, f"{st}")
    resid = gen.residual_diff(w.pri, w.sbx[0], ["big"])
    ok(not resid, f"residual must be empty, got {len(resid)}")
    sample = {**cs, "data_delta": cs["data_delta"][:200]}
    ok(not gen.verify_conformance(w.pri, sample), "sampled conformance")
    print(f"\n      [P01] agent work {t_work*1000:.0f}ms | capture {t_cap*1000:.0f}ms"
          f" | apply {t_apply*1000:.0f}ms (lock {r.ms:.0f}ms) | "
          f"journal={st['journal_rows']} delta={st['delta_rows']} "
          f"(17 insert-then-delete netted out) on a 30k-row table", end=" ")
    w.close()


# ============================================ R: red-team regressions (agent)
# Every case here is a failure an adversarial sub-agent actually produced
# against the previous engine. Classification it used: KILL-A = engine
# declared full success while the primary diverged; KILL-B = wrong/incomplete
# state committed, only a post-hoc net noticed; KILL-C = crash that wedged the
# session (every later capture 500s, so a whole round of agent work is lost).

@case("R01 KILL-A: numeric scale + precision survive the artifact round trip")
def r01():
    w = _typeworld("amt numeric, j jsonb", "(1, 1.5, '{\"rate\": 0.5}')")
    w.go(0, "UPDATE typ SET amt = 1.5000000000, "
            "j = '{\"rate\": 0.1234567890123456789, \"scale\": 1.5000000000}' "
            "WHERE id=1",
            "INSERT INTO typ VALUES (2, 0.1234567890123456789, "
            "'{\"pi\": 3.14159265358979311599796346854}')",
            "INSERT INTO typ VALUES (3, 100.000, '{}')",
            # a JSON null NESTED in an object is fine — only a top-level
            # scalar null in the column is ambiguous with SQL NULL (H02)
            "INSERT INTO typ VALUES (4, 2.7182818284590452353602874713527, "
            "'{\"nil\": null}')")
    cs = cap.capture(w.sbx[0])
    ok(len(cs["data_delta"]) == 4,
       f"the id=1 change is scale-only (1.5 -> 1.5000000000) and jsonb '=' "
       f"cannot see it: {cs['stats']}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    # PostgreSQL's own text rendering — no Python decode can launder this
    got = {r["id"]: r["t"] for r in cap.rows(
        w.pri, "SELECT id, amt::text t FROM typ ORDER BY id")}
    ok(got == {1: "1.5000000000", 2: "0.1234567890123456789", 3: "100.000",
               4: "2.7182818284590452353602874713527"}, f"{got}")
    j1 = cap.rows(w.pri, "SELECT j::text t FROM typ WHERE id=1")[0]["t"]
    ok(j1 == '{"rate": 0.1234567890123456789, "scale": 1.5000000000}', j1)
    w.close()


@case("R02 KILL-B: numerics beyond float64 range/precision")
def r02():
    w = _typeworld("amt numeric, j jsonb", "(1, 1, '{}')")
    w.go(0, "UPDATE typ SET amt = 9999999999999999999999.99999, "
            "j = '{\"big\": 123456789012345678901234567890.5}' WHERE id=1",
            "INSERT INTO typ VALUES (2, 1e-40, '{\"tiny\": 0.00000000000000000001}')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    got = [r["t"] for r in cap.rows(w.pri, "SELECT amt::text t FROM typ ORDER BY id")]
    ok(got == ["9999999999999999999999.99999", "0.0000000000000000000000000000000000000001"],
       f"{got}")
    ok(cap.rows(w.pri, "SELECT j::text t FROM typ WHERE id=1")[0]["t"]
       == '{"big": 123456789012345678901234567890.5}', "jsonb big number")
    w.close()


@case("R03 KILL-B: net-zero ALTER TYPE round trip that silently rounds rows")
def r03():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ALTER COLUMN amount TYPE numeric(12,1)",
            "ALTER TABLE orders ALTER COLUMN amount TYPE numeric(10,2)")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal — empty changeset for a "
                                         "session that rewrote every amount")
    except cap.SchemaChanged as e:
        ok("type change" in str(e) or "REWRITTEN" in str(e), str(e)[:140])
    w.close()


@case("R04 KILL-B: same-type USING rewrite (catalog is byte-identical)")
def r04():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE orders ALTER COLUMN note TYPE text USING left(note,3)")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("REWRITTEN" in str(e), str(e)[:140])
    w.close()


@case("R05 KILL-B: DROP COLUMN + re-ADD same name/type erases values")
def r05():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers DROP COLUMN tier",
            "ALTER TABLE customers ADD COLUMN tier text",
            "UPDATE customers SET name = name WHERE id = 1")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal — net catalog diff is empty")
    except cap.SchemaChanged as e:
        ok("rename?" in str(e), str(e)[:140])
    w.close()


@case("R06 KILL-B: three-way RENAME cycle swaps two columns' data")
def r06():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers RENAME COLUMN name TO wftmp",
            "ALTER TABLE customers RENAME COLUMN tier TO name",
            "ALTER TABLE customers RENAME COLUMN wftmp TO tier")
    try:
        cap.capture(w.sbx[0])
        ok(False, "no refusal — would move NOT NULL onto the wrong column")
    except cap.SchemaChanged as e:
        ok("rename?" in str(e), str(e)[:140])
    w.close()


@case("R07 KILL-B: table dropped and recreated inside the session")
def r07():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "DELETE FROM order_items", "DROP TABLE order_items",
            "CREATE TABLE order_items(order_id bigint, sku text, qty int NOT NULL, "
            "PRIMARY KEY (order_id, sku))")
    try:
        cap.capture(w.sbx[0]); ok(False, "no refusal")
    except cap.SchemaChanged as e:
        ok("dropped and recreated" in str(e), str(e)[:140])
    w.close()


@case("R08 KILL-C: hostile identifiers (mixed case, space, reserved, quote)")
def r08():
    w = W('CREATE TABLE "Ship Log"(id bigint PRIMARY KEY, "Carrier Name" text,'
          ' "we""ird" int, "select" text);'
          "INSERT INTO \"Ship Log\" VALUES (1,'DHL',1,'a'),(2,'UPS',2,'b');",
          ["Ship Log"])
    w.go(0, 'UPDATE "Ship Log" SET "Carrier Name" = \'FedEx\', "we""ird" = 9 '
            'WHERE id = 1',
            'DELETE FROM "Ship Log" WHERE id = 2',
            'INSERT INTO "Ship Log" VALUES (3,\'GLS\',3,\'c\')',
            'CREATE TABLE "order"("Select" bigint PRIMARY KEY, "from" text)',
            'INSERT INTO "order" VALUES (1,\'x\')',
            'ALTER TABLE "Ship Log" ADD COLUMN "Tracking#" text DEFAULT \'n/a\'')
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs, tables=["Ship Log", "order"])
    # the down-casing bug created a DIFFERENT table on the primary
    ok(cap.rows(w.pri, "SELECT to_regclass('\"Ship Log\"') IS NOT NULL a, "
                       "to_regclass('shiplog') IS NULL b")[0] == {"a": True, "b": True},
       "exact-name table on the primary, no down-cased twin")
    w.close()


@case("R09 KILL-C: columns named like the engine's own SQL aliases")
def r09():
    w = _typeworld('"_wfc" text, o text, n text, s text, r text, j text, '
                   'col text, cn text, pk text',
                   "(1,'a','b','c','d','e','f','g','h','i')")
    w.go(0, "UPDATE typ SET _wfc='A', o='B', n='C', s='D', r='E', j='F', "
            "col='G', cn='H', pk='I' WHERE id=1",
            "INSERT INTO typ VALUES (2,'z','z','z','z','z','z','z','z','z')",
            "ALTER TABLE typ ADD COLUMN _wfk text DEFAULT 'shadow'")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    ok(cap.rows(w.pri, "SELECT _wfc FROM typ WHERE id=1")[0]["_wfc"] == "A",
       "the shadowing column's value, not the row image")
    w.close()


@case("R10 net-zero add/drop of a SESSION-ONLY column stays legal")
def r10():
    # the rename guard must not fire on a column that never existed in the
    # basis and does not exist in the final state — no data can be lost
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "ALTER TABLE customers ADD COLUMN scratch_note text DEFAULT 'x'",
            "UPDATE customers SET scratch_note = 'y' WHERE id = 1",
            "ALTER TABLE customers DROP COLUMN scratch_note",
            "UPDATE customers SET tier = 'platinum' WHERE id = 1")
    cs = cap.capture(w.sbx[0])
    ok(not cs["schema_delta"], f"net-zero schema: {cs['schema_delta']}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    w.close()


# ==================================== G: server-generated column attributes
# The catalog snapshot used to model identity / GENERATED ... STORED /
# COLLATE columns as plain ones: a generated column's generation expression
# was emitted as a DEFAULT (apply died on "cannot use column reference in
# DEFAULT expression"), and identity and collation were silently dropped —
# the primary got a structurally different table with no signal at all.
@case("G01 new table with identity + generated + collation is reproduced")
def g01():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, 'CREATE TABLE invoice(id bigint GENERATED ALWAYS AS IDENTITY '
            'PRIMARY KEY, base numeric(10,2) NOT NULL, '
            'tax numeric(10,2) GENERATED ALWAYS AS (base * 0.2) STORED, '
            'nm text COLLATE "C" NOT NULL)',
            "INSERT INTO invoice(base, nm) VALUES (100.00,'b'),(250.00,'A')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs, tables=SCOPE_STD + ["invoice"])
    r = cap.rows(w.pri, """
        SELECT a.attname c, a.attidentity i, a.attgenerated g,
               pg_get_expr(d.adbin, d.adrelid) e,
               (SELECT collname FROM pg_collation WHERE oid=a.attcollation) coll
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid='invoice'::regclass AND a.attnum>0
        ORDER BY a.attnum""")
    by = {x["c"]: x for x in r}
    ok(by["id"]["i"] == "a", f"identity lost: {by['id']}")
    ok(by["tax"]["g"] == "s" and "base" in by["tax"]["e"],
       f"generated lost: {by['tax']}")
    ok(by["nm"]["coll"] == "C", f"collation lost: {by['nm']}")
    ok([str(x["t"]) for x in cap.rows(
        w.pri, "SELECT tax t FROM invoice ORDER BY id")] == ["20.00", "50.00"],
       "generated values recomputed by the primary")
    w.close()


@case("G02 identity sequence is advanced past the merged keys")
def g02():
    # the merged rows carry the sandbox's ids; if the primary's sequence stays
    # at 0 the next ordinary INSERT collides on the primary key
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, 'CREATE TABLE tick(id bigint GENERATED BY DEFAULT AS IDENTITY '
            'PRIMARY KEY, v text)',
            "INSERT INTO tick(v) VALUES ('a'),('b'),('c')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    with w.pri.cursor() as c:
        c.execute("INSERT INTO tick(v) VALUES ('d') RETURNING id")
        nxt = c.fetchone()[0]
    w.pri.commit()
    ok(nxt == 4, f"next identity value should clear the merged keys, got {nxt}")
    w.close()


@case("G03 serial sequence on an EXISTING table is advanced too")
def g03():
    w = W("CREATE TABLE ev(id bigserial PRIMARY KEY, v text);"
          "INSERT INTO ev(v) VALUES ('a'),('b');", ["ev"])
    w.go(0, "INSERT INTO ev(v) VALUES ('c'),('d'),('e')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    with w.pri.cursor() as c:
        c.execute("INSERT INTO ev(v) VALUES ('f') RETURNING id")
        nxt = c.fetchone()[0]
    w.pri.commit()
    ok(nxt == 6, f"primary's serial must skip the merged ids, got {nxt}")
    w.close()


@case("G04 DML on a table with a generated column round-trips")
def g04():
    # 'line' is also a pg_catalog TYPE, so this doubles as a shadowing case:
    # an unqualified NULL::"line" binds to the geometric type (see G07)
    w = W("CREATE TABLE line(id bigint PRIMARY KEY, qty int NOT NULL, "
          "price numeric(10,2) NOT NULL, "
          "total numeric(12,2) GENERATED ALWAYS AS (qty * price) STORED);"
          "INSERT INTO line(id,qty,price) VALUES (1,2,10.00),(2,3,5.00);",
          ["line"])
    w.go(0, "UPDATE line SET qty = 7 WHERE id = 1",
            "INSERT INTO line(id,qty,price) VALUES (3,4,2.50)",
            "DELETE FROM line WHERE id = 2")
    cs = cap.capture(w.sbx[0])
    ok(all("total" not in d["changed"] for d in cs["data_delta"]),
       "a generated column is never a merged value")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    ok([str(x["t"]) for x in cap.rows(
        w.pri, "SELECT total t FROM line ORDER BY id")] == ["70.00", "10.00"],
       "primary recomputed the generated column from the merged bases")
    w.close()


@case("G05 refusals: adding identity/generated, or changing one, on an "
      "existing table")
def g05():
    for stmt, want in [
        ("ALTER TABLE customers ADD COLUMN seq bigint GENERATED BY DEFAULT "
         "AS IDENTITY", "backfills every row implicitly"),
        ("ALTER TABLE orders ADD COLUMN dbl numeric(10,2) GENERATED ALWAYS "
         "AS (amount * 2) STORED", "backfills every row implicitly"),
        ("ALTER TABLE customers ALTER COLUMN name TYPE text COLLATE \"C\"",
         "collation changed"),
    ]:
        w = W(SEED_STD, SCOPE_STD)
        w.go(0, stmt)
        try:
            cap.capture(w.sbx[0])
            ok(False, f"no refusal for: {stmt}")
        except cap.SchemaChanged as e:
            ok(want in str(e), f"{stmt} -> {str(e)[:160]}")
        w.close()


@case("G06 ALWAYS-identity keys on an EXISTING table survive the merge")
def g06():
    # the primary rejects an explicit value for an ALWAYS identity column
    # unless the INSERT says OVERRIDING SYSTEM VALUE — without it the merge
    # either errors or, worse, renumbers rows that FKs elsewhere point at
    w = W("CREATE TABLE tag(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
          " v text NOT NULL);"
          "INSERT INTO tag(v) VALUES ('a'),('b'),('c');", ["tag"])
    w.go(0, "INSERT INTO tag(v) VALUES ('d'),('e')",
            "UPDATE tag SET v = 'A' WHERE id = 1",
            "DELETE FROM tag WHERE id = 2")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    ok([x["id"] for x in cap.rows(w.pri, "SELECT id FROM tag ORDER BY id")]
       == [1, 3, 4, 5], "the sandbox's own identity keys, not renumbered ones")
    w.close()


@case("G07 tables whose names shadow pg_catalog types and relations")
def g07():
    # pg_catalog is implicitly FIRST in the search path. Unqualified, "point"
    # and "money" are built-in TYPES (so NULL::"point" is not the table's
    # rowtype) and "pg_class" is the catalog relation — the engine would read
    # and write somewhere other than the agent's table.
    w = W('CREATE TABLE point(id bigint PRIMARY KEY, v text);'
          'CREATE TABLE money(id bigint PRIMARY KEY, v numeric(10,2));'
          'CREATE TABLE pg_class(id bigint PRIMARY KEY, v text);'
          "INSERT INTO public.point VALUES (1,'a');"
          "INSERT INTO public.money VALUES (1, 1.00);"
          "INSERT INTO public.pg_class VALUES (1,'a');",
          ["point", "money", "pg_class"])
    w.go(0, "INSERT INTO public.point VALUES (2,'b')",
            "UPDATE public.money SET v = 2.50 WHERE id = 1",
            "DELETE FROM public.pg_class WHERE id = 1",
            "INSERT INTO public.pg_class VALUES (2,'c')",
            'CREATE TABLE "interval"(id bigint PRIMARY KEY, v text)',
            "INSERT INTO public.\"interval\" VALUES (1,'x')")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs, tables=["point", "money", "pg_class", "interval"])
    w.close()


# ========================== H: adversarial round 2 (values the row model,
# ========================== objects the catalog model, and DDL that hides
# ========================== outside both of them)
def _refused(fn, *needles):
    try:
        fn()
    except (cap.SchemaChanged, RuntimeError) as e:
        ok(any(n in str(e) for n in needles), str(e)[:220])
        return str(e)
    ok(False, f"no refusal (expected one of {needles})")


@case("H01 KILL: table renamed / moved out of the window is NOT a drop")
def h01():
    for stmt in ("ALTER TABLE order_items SET SCHEMA arch",
                 "ALTER TABLE order_items RENAME TO scratch_items",
                 "ALTER TABLE order_items RENAME TO wf_items"):
        w = W(SEED_STD, SCOPE_STD)
        w.go(0, "CREATE SCHEMA IF NOT EXISTS arch", stmt)
        # without the oid check this captures as drop_table and DELETEs the
        # primary's rows while both verifiers report success
        _refused(lambda: cap.capture(w.sbx[0]),
                 "renamed or moved out", "unsupported DDL")
        w.close()


@case("H02 KILL: a jsonb scalar null is not an SQL NULL")
def h02():
    w = _typeworld("j jsonb", "(1, '{}'), (2, NULL)")
    w.go(0, "UPDATE typ SET j = 'null' WHERE id = 1")
    _refused(lambda: cap.capture(w.sbx[0]), "JSON null scalar")
    w.close()
    # the reverse direction (jsonb null -> SQL NULL) renders identically on
    # both sides of capture's own before/after text compare, so it yields no
    # delta row at all — it can only be caught on the BASIS, at install
    _refused(lambda: _typeworld("j jsonb", "(1, 'null')"), "JSON null scalar")


@case("H03 KILL: net-zero DROP+ADD of the LAST column keeps stale prod values")
def h03():
    w = W("CREATE TABLE inventory(sku text PRIMARY KEY, stock int NOT NULL);"
          "INSERT INTO inventory VALUES ('a',100),('b',40),('c',7);",
          ["inventory"])
    # every gate passed before: fingerprint is identical (last column, so no
    # ordinal moves), the change is metadata-only (relfilenode unchanged), and
    # every row WAS journaled — just after the drop, so their before-images
    # already read stock=0 and 'changed' never mentions stock
    w.go(0, "ALTER TABLE inventory DROP COLUMN stock",
            "ALTER TABLE inventory ADD COLUMN stock int NOT NULL DEFAULT 0",
            "UPDATE inventory SET sku = sku || '!'")
    _refused(lambda: cap.capture(w.sbx[0]), "dropped and re-added")
    w.close()


@case("H04 ADD COLUMN jsonb with a scalar default backfills the right value")
def h04():
    w = _typeworld("s text", "(1, 'x'), (2, 'y')")
    w.go(0, "ALTER TABLE typ ADD COLUMN j jsonb DEFAULT '\"5\"'::jsonb",
            "UPDATE typ SET s = 'z' WHERE id = 1")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    got = [r["t"] for r in cap.rows(w.pri, "SELECT j::text t FROM typ ORDER BY id")]
    ok(got == ['"5"', '"5"'], f"the JSON string \"5\", not the number 5: {got}")
    w.close()
    # …and a value that is not even valid SQL when bound as a bare string
    w = _typeworld("s text", "(1, 'x')")
    w.go(0, "ALTER TABLE typ ADD COLUMN j jsonb DEFAULT '\"hi\"'::jsonb")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    ok(cap.rows(w.pri, "SELECT j::text t FROM typ")[0]["t"] == '"hi"', "jsonb str")
    w.close()


@case("H05 ADD COLUMN text[] with a non-scalar default is backfilled, not refused")
def h05():
    w = _typeworld("s text", "(1, 'x'), (2, 'y')")
    w.go(0, "ALTER TABLE typ ADD COLUMN tags text[] DEFAULT '{}'",
            "UPDATE typ SET tags = '{hot}' WHERE id = 1")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    got = [r["t"] for r in cap.rows(w.pri, "SELECT tags::text t FROM typ ORDER BY id")]
    ok(got == ["{hot}", "{}"], f"{got}")
    w.close()


@case("H06 new table with no PRIMARY KEY is refused, and the session survives")
def h06():
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE TABLE notes(body text)", "INSERT INTO notes VALUES ('hi')")
    _refused(lambda: cap.capture(w.sbx[0]), "no primary key")
    # the refusal must leave the sandbox usable: fix it up and recapture
    w.go(0, "ALTER TABLE notes ADD COLUMN id bigint PRIMARY KEY DEFAULT 1")
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply after fixup")
    clean(w, cs, tables=SCOPE_STD + ["notes"])
    w.close()


@case("H07 partitioned tables are refused at install and at capture")
def h07():
    _refused(lambda: W("CREATE TABLE ev(id bigint, d date, PRIMARY KEY (id,d)) "
                       "PARTITION BY RANGE (d);"
                       "CREATE TABLE ev_1 PARTITION OF ev "
                       "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');",
                       ["ev"]),
             "partitioned tables are out of PoC scope")
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE TABLE ev(id bigint, d date, PRIMARY KEY (id,d)) "
            "PARTITION BY RANGE (d)",
            "CREATE TABLE ev_1 PARTITION OF ev "
            "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')")
    _refused(lambda: cap.capture(w.sbx[0]), "partitioned tables")
    w.close()


@case("H08 array with a non-default lower bound is refused, not silently re-based")
def h08():
    w = _typeworld("a text[]", "(1, '{x,y}')")
    w.go(0, "UPDATE typ SET a = '[2:3]={x,y}' WHERE id = 1")
    _refused(lambda: cap.capture(w.sbx[0]), "non-default lower bound")
    w.close()


@case("H09 float -0.0 is refused (numeric has no signed zero)")
def h09():
    # note (-0.0)::float8 is +0.0 — the literal is numeric, which has no
    # signed zero either; '-0'::float8 is the real thing
    for typ in ("float8", "float4"):
        w = _typeworld(f"f {typ}", "(1, 1.0)")
        w.go(0, f"UPDATE typ SET f = '-0'::{typ} WHERE id = 1")
        _refused(lambda: cap.capture(w.sbx[0]), "negative zero")
        w.close()
        _refused(lambda: _typeworld(f"f {typ}", f"(1, '-0'::{typ})"),
                 "negative zero")


@case("H10 float8 precision survives a hostile extra_float_digits")
def h10():
    w = _typeworld("f float8", "(1, 1.0)")
    a = w.agent(0)
    with a.cursor() as c:
        c.execute("SET extra_float_digits = -3")
        c.execute("UPDATE typ SET f = 1.2345678901234567 WHERE id = 1")
        c.execute("INSERT INTO typ VALUES (2, 9.87654321098765e-10)")
    a.commit(); a.close()
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    got = [r["t"] for r in cap.rows(w.pri, "SELECT f::text t FROM typ ORDER BY id")]
    ok(got == ["1.2345678901234567", "9.87654321098765e-10"], f"{got}")
    w.close()


@case("H11 type json (not jsonb) is refused — at install and for new tables")
def h11():
    _refused(lambda: W("CREATE TABLE j(id bigint PRIMARY KEY, d json);", ["j"]),
             "type json")
    w = W(SEED_STD, SCOPE_STD)
    w.go(0, "CREATE TABLE j(id bigint PRIMARY KEY, d json)",
            "INSERT INTO j VALUES (1, '{\"b\":1,\"a\":2,\"a\":3}')")
    _refused(lambda: cap.capture(w.sbx[0]), "type json")
    w.close()


@case("H12 non-table objects and table attributes cannot ride along silently")
def h12():
    for stmt, needle in (
            ("CREATE VIEW v AS SELECT * FROM customers", "unsupported DDL"),
            ("CREATE SCHEMA s2", "unsupported DDL"),
            ("COMMENT ON TABLE customers IS 'hi'", "unsupported DDL"),
            ("CREATE SEQUENCE s3", "unsupported DDL"),
            ("CREATE INDEX ix ON customers(tier)", "constraint/index"),
            ("ALTER TABLE customers ENABLE ROW LEVEL SECURITY", "'rls'"),
            ("ALTER TABLE customers SET (fillfactor = 70)", "'reloptions'"),
            ("ALTER TABLE customers DISABLE TRIGGER USER", "'triggers'"),
            ("CREATE UNLOGGED TABLE u(id bigint PRIMARY KEY)", "'persistence'")):
        w = W(SEED_STD, SCOPE_STD)
        w.go(0, stmt)
        _refused(lambda: cap.capture(w.sbx[0]), needle)
        w.close()


@case("H13 KILL: session_replication_role cannot silence the journal")
def h13():
    w = W(SEED_STD, SCOPE_STD)
    a = w.agent(0)
    with a.cursor() as c:
        c.execute("SET session_replication_role = 'replica'")
        c.execute("UPDATE customers SET tier = 'plat' WHERE id = 1")
        # NB the replica role disables FK triggers too, so deleting a row with
        # dependents would build a sandbox state the primary must reject
        c.execute("DELETE FROM orders WHERE id = 3")
        c.execute("INSERT INTO customers VALUES (9, 'Zoe', 'new')")
    a.commit(); a.close()
    cs = cap.capture(w.sbx[0])
    ok(len(cs["data_delta"]) == 3, f"all three rows journaled: {cs['stats']}")
    ok(w.apply(cs).outcome == "applied", "apply")
    clean(w, cs)
    w.close()
    # and TRUNCATE stays blocked under the replica role too
    w = W(SEED_STD, SCOPE_STD)
    a = w.agent(0)
    try:
        with a.cursor() as c:
            c.execute("SET session_replication_role = 'replica'")
            c.execute("TRUNCATE order_items")
        ok(False, "TRUNCATE was not blocked")
    except psycopg2.errors.RaiseException as e:
        ok("not capturable" in str(e), str(e)[:120])
    a.close(); w.close()


@case("H14 existing columns cannot be reordered (actionable capture refusal)")
def h14():
    w = W(SEED_STD, SCOPE_STD)
    # rebuild 'tier' after 'name' -> the surviving basis columns move
    w.go(0, "ALTER TABLE customers ADD COLUMN tier2 text",
            "UPDATE customers SET tier2 = tier",
            "ALTER TABLE customers DROP COLUMN tier",
            "ALTER TABLE customers RENAME COLUMN tier2 TO tier")
    _refused(lambda: cap.capture(w.sbx[0]),
             "dropped and re-added", "reordered", "rename?")
    w.close()


@case("H15 residual_diff counts duplicate rows (multiset, not set)")
def h15():
    w = W("CREATE TABLE dup(id bigint PRIMARY KEY, v text);"
          "CREATE TABLE nokey(v text);"
          "INSERT INTO dup VALUES (1,'a');"
          "INSERT INTO nokey VALUES ('x'),('x'),('x');", ["dup"])
    w.go(0, "UPDATE dup SET v = 'b' WHERE id = 1")
    with w.pri.cursor() as c:                       # prod drifts: 3 copies -> 1
        c.execute("DELETE FROM nokey WHERE ctid <> (SELECT min(ctid) FROM nokey)")
    w.pri.commit()
    cs = cap.capture(w.sbx[0])
    ok(w.apply(cs).outcome == "applied", "apply")
    resid = gen.residual_diff(w.pri, w.sbx[0], ["dup", "nokey"])
    ok(len(resid) == 2 and all(k == "sandbox-only" for _, k, _ in resid),
       f"two surplus copies in the sandbox, not zero: {resid}")
    w.close()


# ================================================================= runner
if __name__ == "__main__":
    t_all = time.monotonic()
    passed, failed = 0, []
    for name, fn in CASES:
        t0 = time.monotonic()
        try:
            fn()
            passed += 1
            print(f"PASS {name}  ({(time.monotonic()-t0)*1000:.0f}ms)")
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed}/{len(CASES)} passed in {time.monotonic()-t_all:.1f}s"
          + (f" — FAILURES: {failed}" if failed else " — ALL GREEN"))
    raise SystemExit(1 if failed else 0)
