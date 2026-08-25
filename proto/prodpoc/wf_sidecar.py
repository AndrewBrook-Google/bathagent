"""Wildfire capture sidecar — the only trusted Wildfire code on a replica.

Runs NEXT TO the sandbox PostgreSQL (same container in prod), owns the
capture lifecycle. Design properties:

- Stateless daemon: every bit of session state (scope, basis catalog,
  journal, freeze state) lives in the sandbox DB itself (wf.meta/wf.journal),
  so a sidecar restart loses nothing.
- Service-only surface: in prod these endpoints are reachable exclusively by
  the Wildfire control plane (managed: direct call w/ OIDC; BYO: dial-out
  command channel). There is NO client/agent-facing interface by design —
  the agent only ever talks SQL to the sandbox.
- Freeze is advisory-but-real: DB-level default_transaction_read_only=on +
  terminating live connections. A hostile superuser agent could still write;
  merge safety never depends on this (OCC + EUC at apply do), freeze exists
  so the captured artifact provably matches what the reviewer sees.

API (v1):
  GET  /healthz
  GET  /v1/session               state / scope / basis / journal size
  POST /v1/session/start         {scope:[...]}       install triggers+basis
  POST /v1/freeze                stop writes, terminate live conns
  POST /v1/unfreeze              re-enable writes (reject/fix-up path)
  POST /v1/capture               {freeze:bool, inline_max:int?} -> artifact,
                                 inline if small, staged-to-file otherwise
                                 (PoC: file staging simulates the bucket)
  GET  /v1/artifact/{digest}     fetch a staged artifact (PoC convenience;
                                 prod reads the bucket, not the sidecar)
  DELETE /v1/session             release (post-merge scale-to-zero)

Run:  WF_SBX_DSN="host=... dbname=..." uvicorn wf_sidecar:app --port 55501
"""
import hashlib
import os
import pathlib

import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import wf_capture as cap
import wf_json as json

DSN = os.environ["WF_SBX_DSN"]
DBNAME = dict(kv.split("=", 1) for kv in DSN.split())["dbname"]
STAGING = pathlib.Path(os.environ.get("WF_STAGING", "/tmp/wf_staging"))
INLINE_MAX = int(os.environ.get("WF_INLINE_MAX", 64 * 1024))

app = FastAPI(title="wf-sidecar", version="0.1")


def conn(autocommit=False):
    c = psycopg2.connect(DSN)
    c.autocommit = autocommit
    with c.cursor() as cur:
        # the sidecar overrides the freeze for its own (read-mostly) sessions
        cur.execute("SET default_transaction_read_only = off")
    if not autocommit:
        c.commit()
    return c


def state_of(c):
    if not cap.rows(c, "SELECT 1 FROM information_schema.schemata "
                       "WHERE schema_name='wf'"):
        return None
    r = cap.rows(c, "SELECT v FROM wf.meta WHERE k='state'")
    return r[0]["v"] if r else None


def set_state(c, s):
    cap.run(c, "INSERT INTO wf.meta VALUES ('state', %s) "
               "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (json.dumps(s),))
    c.commit()


def do_freeze():
    a = conn(autocommit=True)
    with a.cursor() as cur:
        cur.execute(f'ALTER DATABASE "{DBNAME}" '
                    f'SET default_transaction_read_only = on')
        cur.execute("""SELECT count(pg_terminate_backend(pid))
                       FROM pg_stat_activity
                       WHERE datname = %s AND pid <> pg_backend_pid()""",
                    (DBNAME,))
        n = cur.fetchone()[0]
    a.close()
    c = conn()
    set_state(c, "frozen")
    c.close()
    return n


def jresp(payload):
    """Serialize responses ourselves. FastAPI's encoder maps Decimal to float,
    which would re-introduce at the HTTP hop exactly the numeric-scale loss
    capture works to avoid (an inline artifact's 118.00 arrives as 118.0)."""
    return Response(json.dumps(payload), media_type="application/json")


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": DBNAME}


@app.get("/v1/session")
def session():
    c = conn()
    st = state_of(c)
    if st is None:
        c.close()
        raise HTTPException(404, "no capture session on this replica")
    meta = {r["k"]: r["v"] for r in cap.rows(c, "SELECT k, v FROM wf.meta")}
    jn = cap.rows(c, "SELECT count(*) n FROM wf.journal")[0]["n"]
    c.close()
    return {"state": st, "scope": meta.get("scope"),
            "basis_fp": meta.get("basis_fp"), "journal_rows": jn}


class StartReq(BaseModel):
    scope: list


@app.post("/v1/session/start")
def start(req: StartReq):
    c = conn()
    try:
        basis = cap.install(c, req.scope)
    except RuntimeError as e:
        c.close()
        raise HTTPException(409, str(e))
    set_state(c, "capturing")
    c.close()
    return {"state": "capturing", "scope": req.scope, "basis_fp": basis}


@app.post("/v1/freeze")
def freeze():
    c = conn()
    if state_of(c) is None:
        c.close()
        raise HTTPException(404, "no session")
    c.close()
    return {"state": "frozen", "terminated_connections": do_freeze()}


@app.post("/v1/unfreeze")
def unfreeze():
    """Reject / merge_failed path: writes re-enabled, journal continues
    against the SAME basis — fix up and re-propose."""
    a = conn(autocommit=True)
    with a.cursor() as cur:
        cur.execute(f'ALTER DATABASE "{DBNAME}" '
                    f'RESET default_transaction_read_only')
    a.close()
    c = conn()
    if state_of(c) is None:
        c.close()
        raise HTTPException(404, "no session")
    set_state(c, "capturing")
    c.close()
    return {"state": "capturing"}


class CaptureReq(BaseModel):
    freeze: bool = True
    inline_max: int = None


@app.post("/v1/capture")
def capture(req: CaptureReq):
    c = conn()
    st = state_of(c)
    c.close()
    if st is None:
        raise HTTPException(404, "no session")
    if st != "frozen":
        if not req.freeze:
            raise HTTPException(409, "session is not frozen and freeze=false")
        do_freeze()
    try:
        art = cap.capture(conn())
    except cap.SchemaChanged as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        # never wedge the session on an unexpected fault: the caller gets a
        # structured refusal it can act on, not an opaque 500
        raise HTTPException(409, f"capture failed on this replica "
                                 f"({type(e).__name__}: {e}) — resync and redo")
    blob = json.dumps(art, sort_keys=True).encode()
    digest = hashlib.sha256(blob).hexdigest()
    limit = INLINE_MAX if req.inline_max is None else req.inline_max
    if len(blob) <= limit:
        return jresp({"mode": "inline", "digest": digest, "bytes": len(blob),
                      "stats": art["stats"], "artifact": art})
    STAGING.mkdir(parents=True, exist_ok=True)
    (STAGING / f"{digest}.json").write_bytes(blob)
    return jresp({"mode": "staged", "digest": digest, "bytes": len(blob),
                  "stats": art["stats"],
                  "artifact_ref": f"file://{STAGING}/{digest}.json"})


@app.get("/v1/artifact/{digest}")
def artifact(digest: str):
    p = STAGING / f"{digest}.json"
    if not p.exists():
        raise HTTPException(404, "unknown artifact")
    return Response(p.read_bytes(), media_type="application/json")


@app.delete("/v1/session")
def release():
    """Post-merge: the reader scales to zero. PoC: mark + kick connections."""
    c = conn()
    if state_of(c) is None:
        c.close()
        raise HTTPException(404, "no session")
    set_state(c, "released")
    c.close()
    a = conn(autocommit=True)
    with a.cursor() as cur:
        cur.execute("""SELECT count(pg_terminate_backend(pid))
                       FROM pg_stat_activity
                       WHERE datname = %s AND pid <> pg_backend_pid()""",
                    (DBNAME,))
    a.close()
    return {"state": "released"}
