"""One merge cycle for the agent-driven test: sidecar capture -> apply ->
engine-side verification. Usage:
    python3 merge_once.py <primary_db> <sandbox_db> <scope_csv> <expect_residual>
Prints a JSON summary; exit 1 on any failure.
"""
import hashlib
import wf_json as json
import sys
import urllib.request
import uuid

import psycopg2

import wf_sqlgen as gen

SIDE = "http://127.0.0.1:55501"
pri_db, sbx_db, scope_csv, expect_residual = sys.argv[1:5]
scope = scope_csv.split(",")


def api(path, body=None):
    req = urllib.request.Request(
        SIDE + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


r = api("/v1/capture", {"freeze": True})
if r["mode"] == "staged":
    blob = urllib.request.urlopen(f"{SIDE}/v1/artifact/{r['digest']}").read()
    assert hashlib.sha256(blob).hexdigest() == r["digest"], "digest mismatch"
    art = json.loads(blob)
else:
    art = r["artifact"]

pri = psycopg2.connect(f"host=127.0.0.1 port=55442 dbname={pri_db} "
                       f"user=postgres password=wf")
res = gen.apply_staged(pri, art, str(uuid.uuid4()))
out = {"capture_mode": r["mode"], "stats": r["stats"],
       "schema_ops": [f"{o['op']}:{o['table']}" + (f".{o['col']}" if 'col' in o else "")
                      for o in art["schema_delta"]],
       "apply": {"outcome": res.outcome, "rows": res.applied_rows,
                 "lock_ms": round(res.ms, 1), "detail": res.detail}}
failed = res.outcome != "applied"

if not failed:
    import wf_capture as cap
    bad = gen.verify_conformance(pri, art)
    sbx = psycopg2.connect(f"host=127.0.0.1 port=55443 dbname={sbx_db} "
                           f"user=postgres password=wf")
    created = {o["table"] for o in art["schema_delta"] if o["op"] == "create_table"}
    dropped = {o["table"] for o in art["schema_delta"] if o["op"] == "drop_table"}
    tables = sorted(({d["relid"] for d in art["data_delta"]}
                     | set(scope) | created) - dropped)
    resid = gen.residual_diff(pri, sbx, [t for t in tables])
    # schema-shape net: every table in the sandbox catalog must match primary
    pc, sc = cap.catalog_snapshot(pri), cap.catalog_snapshot(sbx)
    shape = sorted(t for t in sc if pc.get(t) != sc[t])
    out["conformance_violations"] = len(bad)
    out["residual"] = len(resid)
    out["residual_sample"] = [f"{t} {side}: {row[:90]}" for t, side, row in resid[:6]]
    out["schema_shape_mismatch"] = shape
    failed = bool(bad) or len(resid) != int(expect_residual) or bool(shape)

print(json.dumps(out, indent=1, default=str))
sys.exit(1 if failed else 0)
