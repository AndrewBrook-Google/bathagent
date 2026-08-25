"""Integration test: the COMPLETE flow with a real sidecar process.

  replica clone (simulated attach->detach)
    -> sidecar: POST /v1/session/start      (triggers + basis snapshot)
    -> agent works over plain SQL           (mixed DDL + DML)
    -> propose#1: POST /v1/capture          (freeze + staged artifact)
         frozen replica refuses agent writes
    -> human REJECTS -> POST /v1/unfreeze -> agent fixes up
    -> sidecar killed + restarted           (state survives in the DB)
    -> concurrent drift lands on primary
    -> propose#2: POST /v1/capture          (inline artifact)
    -> service applies artifact#2 on primary (three-phase, OCC)
    -> verify: conformance + residual == exactly the drift + fingerprints
    -> stale artifact#1 refused (schema_drift), duplicate apply refused
    -> DELETE /v1/session                   (release)

This file plays the Wildfire SERVICE: it talks to the sidecar over HTTP only
(never imports wf_capture) and runs the merge engine against the primary.
"""
import hashlib
import wf_json as json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid

import psycopg2

import wf_sqlgen as gen

PP, SP, SIDE_PORT = 55442, 55443, 55501
SIDE = f"http://127.0.0.1:{SIDE_PORT}"
STAGING = "/tmp/wf_staging_int"
SCOPE = ["customers", "orders", "order_items"]

SEED = pathlib.Path(__file__).with_name("run_poc.py").read_text().split(
    'SEED = """')[1].split('"""')[0]


def dsn(port, db):
    return f"host=127.0.0.1 port={port} dbname={db} user=postgres password=wf"


def admin(port, sql):
    c = psycopg2.connect(dsn(port, "postgres"))
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(sql)
    c.close()


def api(path, body=None, method=None):
    req = urllib.request.Request(
        SIDE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def api_expect_error(path, body=None, method=None, code=409):
    try:
        api(path, body, method)
    except urllib.error.HTTPError as e:
        assert e.code == code, f"expected {code}, got {e.code}"
        return json.loads(e.read()).get("detail", "")
    raise AssertionError(f"expected HTTP {code}, call succeeded")


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name}")


def section(t):
    print(f"\n{'=' * 64}\n{t}\n{'=' * 64}")


def start_sidecar():
    # a leftover sidecar from an aborted run would answer /healthz while the
    # new process fails to bind — the test would then silently exercise STALE
    # engine code (this cost a debugging round; keep the guard)
    try:
        api("/healthz")
        raise SystemExit(f"port {SIDE_PORT} already serving a sidecar — "
                         f"kill it first: pkill -f 'uvicorn wf_sidecar'")
    except urllib.error.URLError:
        pass
    env = {**os.environ, "WF_SBX_DSN": dsn(SP, "int_sbx"),
           "WF_STAGING": STAGING, "WF_INLINE_MAX": str(64 * 1024)}
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wf_sidecar:app",
         "--port", str(SIDE_PORT), "--host", "127.0.0.1", "--log-level", "warning"],
        env=env, cwd=pathlib.Path(__file__).parent,
        stdout=open("/tmp/wf_sidecar_int.log", "ab"),
        stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            api("/healthz")
            return p
        except Exception:
            time.sleep(0.25)
    raise SystemExit("sidecar did not come up — see /tmp/wf_sidecar_int.log")


def rows(conn, sql, args=None):
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- phase A
section("A — world: primary seeded, replica cloned, sidecar up")
admin(PP, "DROP DATABASE IF EXISTS int_pri WITH (FORCE)")
admin(PP, "CREATE DATABASE int_pri")
pri = psycopg2.connect(dsn(PP, "int_pri"))
with pri.cursor() as c:
    c.execute(SEED)
pri.commit()
admin(SP, "DROP DATABASE IF EXISTS int_sbx WITH (FORCE)")
admin(SP, "CREATE DATABASE int_sbx")
subprocess.run(
    f"PGPASSWORD=wf pg_dump -h 127.0.0.1 -p {PP} -U postgres --no-owner "
    f"--no-acl -d int_pri | "
    f"PGPASSWORD=wf psql -q -h 127.0.0.1 -p {SP} -U postgres -d int_sbx",
    shell=True, check=True, capture_output=True)
side = start_sidecar()
check("sidecar healthy", api("/healthz")["ok"])
check("no session yet -> 404", "no capture session" in
      api_expect_error("/v1/session", code=404))

# ---------------------------------------------------------------- phase B
section("B — session start (detach): triggers + basis snapshot via API")
r = api("/v1/session/start", {"scope": SCOPE})
check("state=capturing, basis fingerprints returned",
      r["state"] == "capturing" and set(r["basis_fp"]) == set(SCOPE))
check("double start refused",
      "already installed" in api_expect_error("/v1/session/start",
                                              {"scope": SCOPE}))

# agent: a completely ordinary connection, no wrapper anywhere
agent = psycopg2.connect(dsn(SP, "int_sbx"))
with agent.cursor() as c:
    c.execute("ALTER TABLE orders ADD COLUMN pts int")
    c.execute("UPDATE orders SET pts = floor(amount/10)")
    c.execute("ALTER TABLE orders ALTER COLUMN pts SET NOT NULL")
    c.execute("CREATE TABLE loyalty_tiers(id bigint PRIMARY KEY, "
              "name text NOT NULL, min_points int NOT NULL)")
    c.execute("INSERT INTO loyalty_tiers VALUES (1,'bronze',0),(2,'silver',10)")
    c.execute("UPDATE orders SET amount = 115.00 WHERE id = 1")
agent.commit()
agent.close()
check("journal filling", api("/v1/session")["journal_rows"] > 0,
      f"journal={api('/v1/session')['journal_rows']}")

# ---------------------------------------------------------------- phase C
section("C — propose #1: freeze + capture (forced staged transfer)")
r1 = api("/v1/capture", {"freeze": True, "inline_max": 0})
check("staged mode with digest + ref", r1["mode"] == "staged" and r1["digest"],
      f"{r1['bytes']}B, {r1['stats']}")
check("session frozen", api("/v1/session")["state"] == "frozen")
try:
    a2 = psycopg2.connect(dsn(SP, "int_sbx"))
    with a2.cursor() as c:
        c.execute("UPDATE orders SET amount = 1 WHERE id = 1")
    a2.commit()
    check("frozen replica refuses agent writes", False)
except psycopg2.errors.ReadOnlySqlTransaction:
    check("frozen replica refuses agent writes", True)

# the service fetches the staged artifact (prod: from the bucket) + verifies
blob = urllib.request.urlopen(f"{SIDE}/v1/artifact/{r1['digest']}").read()
check("digest verifies (integrity)",
      hashlib.sha256(blob).hexdigest() == r1["digest"])
art1 = json.loads(blob)
check("artifact carries schema+data deltas",
      len(art1["schema_delta"]) == 3 and len(art1["data_delta"]) == 6,
      f"ddl={len(art1['schema_delta'])} rows={len(art1['data_delta'])}")

# ---------------------------------------------------------------- phase D
section("D — human REJECTS: unfreeze, agent fixes up (same basis)")
api("/v1/unfreeze", {})
a3 = psycopg2.connect(dsn(SP, "int_sbx"))
with a3.cursor() as c:
    c.execute("UPDATE orders SET amount = 118.00 WHERE id = 1")   # revised
    c.execute("INSERT INTO loyalty_tiers VALUES (3,'gold',25)")
a3.commit()
a3.close()
check("unfrozen replica accepts writes again", True)

# ---------------------------------------------------------------- phase E
section("E — sidecar restart: all state survives in the replica DB")
side.terminate()
side.wait(timeout=10)
side = start_sidecar()
s = api("/v1/session")
check("state, scope and journal intact after restart",
      s["state"] == "capturing" and s["scope"] == SCOPE
      and s["journal_rows"] > 0, f"journal={s['journal_rows']}")

# ---------------------------------------------------------------- phase F
section("F — concurrent drift on primary, then propose #2 (inline)")
with pri.cursor() as c:
    c.execute("UPDATE orders SET note = 'ops touched' WHERE id = 2")
pri.commit()
r2 = api("/v1/capture", {"freeze": True})
check("inline mode", r2["mode"] == "inline", f"{r2['bytes']}B")
art2 = r2["artifact"]
check("cumulative delta vs SAME basis (order 1 before=100, after=118)",
      any(d["relid"] == "orders" and d["pk"] == {"id": 1}
          and float(d["before"]["amount"]) == 100.0
          and float(d["after"]["amount"]) == 118.0
          for d in art2["data_delta"]))

# ---------------------------------------------------------------- phase G
section("G — service merges artifact #2 on the primary")
aid2 = str(uuid.uuid4())
res = gen.apply_staged(pri, art2, aid2)
print("  apply:", res)
check("applied", res.outcome == "applied")
_bad = gen.verify_conformance(pri, art2)
check("delta conformance", not _bad,
      f"{len(art2['data_delta'])} rows checked" + (f"; {_bad[:2]}" if _bad else ""))
check("numeric scale survived capture -> HTTP -> apply",
      rows(pri, "SELECT amount::text t FROM orders WHERE id=1")[0]["t"]
      == "118.00", "FastAPI's own encoder would render this 118.0")
sbx_check = psycopg2.connect(dsn(SP, "int_sbx"))
resid = gen.residual_diff(pri, sbx_check, SCOPE + ["loyalty_tiers"])
for t, side_, row in resid:
    print(f"    residual: {t} {side_}: {row[:100]}")
check("residual == exactly the tolerated drift",
      len(resid) == 2 and all('"id": 2' in row for _, _, row in resid))
r = rows(pri, "SELECT pts, amount FROM orders WHERE id=1")[0]
check("fix-up value won (amount=118; pts=10 from backfill-time amount)",
      float(r["amount"]) == 118.0 and r["pts"] == 10)

# ---------------------------------------------------------------- phase H
section("H — stale artifact #1 and duplicate apply are both refused")
res1 = gen.apply_staged(pri, art1, str(uuid.uuid4()))
check("stale artifact refused (schema drifted since ITS basis)",
      res1.outcome == "schema_drift", res1.detail[:60])
res2 = gen.apply_staged(pri, art2, aid2)
check("duplicate action_id refused (idempotency gate)",
      res2.outcome == "error" and "wf_applied" in res2.detail)

# ---------------------------------------------------------------- phase I
section("I — release (scale-to-zero)")
api("/v1/session", method="DELETE")
check("released", api("/v1/session")["state"] == "released")
side.terminate()

print("\nINTEGRATION TEST: ALL PHASES PASSED")
