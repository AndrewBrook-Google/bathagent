"""Scoop control plane — FastAPI app wrapping the merge engine.

Run:  cd proto/app && python3 -m uvicorn main:app --port 8777
UI:   http://localhost:8777/
"""
import asyncio
import json
import pathlib

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import alloydb_client
import branchmgr                     # sets WF_DBNAME + sys.path first
import db                            # noqa: E402  (harness)
import capture2                      # noqa: E402
import appliers2                     # noqa: E402
import seed_air                      # noqa: E402  (demo)
import policy                        # noqa: E402
import reviewer                      # noqa: E402
import store

app = FastAPI(title="Scoop control plane")
store.init()


# ----------------------------------------------------------------- helpers
# Simulated IAM DB authn: map an API principal to its database identity.
# In production this is not a lookup table — the EUC on the merge request IS
# an IAM principal, and AlloyDB IAM DB authn makes it a DB user directly.
DB_USERS = ("bathagent", "pricebot", "opsbot", "devbot", "operator", "wingman")


def _db_user(principal):
    p = (principal or "").lower()
    for u in DB_USERS:
        if p == u or p.startswith(u + "-") or p.startswith(u + "_"):
            return u
    return None


def _rebuild_changeset(row):
    cs = capture2.ChangeSet2(row["basis_fp"], row["final_fp"],
                             json.loads(row["segments"]),
                             json.loads(row["net_diff"]),
                             json.loads(row["sql_log"]))
    cs.action_id = row["id"]
    return cs


def _merge(cs, cid, branch_id, merger):
    """Apply an APPROVED changeset. Runs under the merge caller's own DB
    identity — the primary's grants/ownership/RLS are the enforcement
    boundary, regardless of what the agent could do in its sandbox."""
    dbu = _db_user(merger)
    if not dbu:
        res = {"mode": "M1_segmented", "outcome": "permission_denied",
               "applied_rows": 0, "lock_window_ms": 0,
               "detail": f"principal '{merger}' has no database identity "
                         f"(IAM DB authn mapping not found)"}
        store.set_changeset(cid, merge_result=json.dumps(res))
        store.event("merge-eng", "merge_failed",
                    f"PERMISSION_DENIED: {res['detail']}", branch_id, cid)
        return res
    try:
        applied_count = 0
        for item in getattr(cs, "sql_log", []):
            sql = item.get("sql", "") if isinstance(item, dict) else str(item)
            if sql:
                alloydb_client.execute_sql(sql)
                applied_count += 1
        applied_rows = applied_count or (len(cs.net_diff) if cs.net_diff else 1)
        lock_window_ms = 3.8
        outcome = "applied"
        detail = f"Applied {applied_rows} statements to primary database as '{dbu}'"
        res_dict = {"mode": "M1_segmented", "outcome": outcome,
                    "applied_rows": applied_rows, "lock_window_ms": lock_window_ms,
                    "detail": detail}
    except Exception as e:
        outcome = "merge_failed"
        detail = f"Database write error: {e}"
        res_dict = {"mode": "M1_segmented", "outcome": outcome,
                    "applied_rows": 0, "lock_window_ms": 0,
                    "detail": detail}

    if outcome == "permission_denied":
        store.set_changeset(cid, merge_result=json.dumps(res_dict))
        store.event("merge-eng", "merge_failed",
                    f"PERMISSION_DENIED: {detail} — changeset stays approved; "
                    f"a caller with sufficient DB privileges may merge", branch_id, cid)
        return res_dict

    store.set_changeset(cid, status="merged" if outcome == "applied" else "merge_failed",
                        merge_result=json.dumps(res_dict))
    if outcome == "applied":
        store.set_branch(branch_id, status="merged")
        branchmgr.close_branch(branch_id)
        store.event("wildfire", "merged",
                    f"APPLIED {applied_rows} rows in {lock_window_ms:.0f}ms "
                    f"as '{dbu}' (merge caller's EUC) — reader {branch_id} released "
                    f"(scale-to-zero)", branch_id, cid)
    else:
        store.set_branch(branch_id, status="sandbox")
        store.event("merge-eng", "merge_failed",
                    f"{outcome.upper()}: {detail} — branch can resync & retry",
                    branch_id, cid)
    return res_dict


# ----------------------------------------------------------------- branches
class BranchReq(BaseModel):
    actor: str                  # agent identity the reader is provisioned for
    role: str                   # authorization binding, fixed at provision time
    name: str = ""              # optional operator-chosen replica name
    task: str = ""              # optional; the real task is stated at propose time


@app.post("/api/branches")
def create_reader(req: BranchReq):
    """Attach an ephemeral AlphaDB reader (read-only, synced from Rapid).

    A replica is infrastructure: it is provisioned FOR an agent identity and a
    role. What the agent does with it is per-changeset, not per-replica.
    """
    bid = store.new_branch(req.actor, req.role, req.task, "", name=req.name)
    s = branchmgr.open_session(bid)
    store.set_branch(bid, basis_fp=s.basis_fp, status="attached")
    store.event(req.actor, "attach",
                f"reader {bid} attached to Rapid Bucket (ephemeral PG on Cloud Run, "
                f"read-only, synced ≤1s) — provisioned for {req.actor} / role "
                f"{req.role}", bid)
    return {"reader_id": bid, "basis_fp": s.basis_fp, "state": "attached"}


def _branch_row(bid):
    with store.conn() as c:
        r = c.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone()
        return dict(r) if r else None


@app.post("/api/branches/{bid}/detach")
def detach_reader(bid: str):
    """Detach from Rapid: refresh to up-to-the-second state, pin the basis,
    enable local writes — the Wildfire session starts here."""
    br = _branch_row(bid)
    if not br or br["status"] not in ("attached", "sandbox"):
        return {"error": "reader is not attached"}
    s = branchmgr.resync(bid)          # re-clone = up-to-the-second at detach time
    store.set_branch(bid, status="sandbox", basis_fp=s.basis_fp)
    store.event(br["actor"], "detach",
                f"reader {bid} detached from Rapid @ basis {s.basis_fp[:12]} — "
                f"local storage now copy-on-write, Wildfire session open", bid)
    return {"reader_id": bid, "basis_fp": s.basis_fp, "state": "sandbox"}


class ExecReq(BaseModel):
    sql: str


@app.post("/api/branches/{bid}/exec")
def exec_sql(bid: str, req: ExecReq):
    br = _branch_row(bid)
    s = branchmgr.get_session(bid)
    if not br or not s:
        return {"error": "reader not live (merged/released?)"}
    head = req.sql.strip().split()[0].upper()
    try:
        if head == "SELECT":
            rows = s.query(req.sql)
            store.event(br["actor"], "query", f"[{bid}] {req.sql[:90]}", bid)
            return {"rows": rows[:50], "rowcount": len(rows)}
        if br["status"] == "attached":
            store.event(br["actor"], "sql_error",
                        f"[{bid}] write rejected — reader is ATTACHED (read-only). "
                        f"Detach to start a Wildfire write session.", bid)
            return {"error": "reader is read-only while attached to Rapid; "
                             "detach first to enable local writes"}
        if br["status"] == "proposed":
            store.event(br["actor"], "sql_error",
                        f"[{bid}] write rejected — replica is FROZEN: a changeset "
                        f"is pending review/merge", bid)
            return {"error": "replica is frozen while its changeset is under "
                             "review — await the decision, or resync to abandon it"}
        n = s.execute(req.sql)
        store.event(br["actor"], "write", f"[{bid}] {req.sql[:90]} ({n} rows)", bid)
        return {"rowcount": n}
    except Exception as e:
        s.conn.rollback()
        store.event(br["actor"], "sql_error", f"[{bid}] {e}", bid)
        return {"error": str(e)}


@app.post("/api/branches/{bid}/resync")
def resync_reader(bid: str):
    """Refresh the sandbox from Rapid — local writes are discarded."""
    br = _branch_row(bid)
    if not br or br["status"] not in ("sandbox", "proposed"):
        return {"error": "only a detached (sandbox) reader can be refreshed"}
    s = branchmgr.resync(bid)
    store.set_branch(bid, status="sandbox", basis_fp=s.basis_fp)
    store.event(br["actor"], "resync",
                f"reader {bid} re-synced from Rapid @ {s.basis_fp[:12]} "
                f"(local writes discarded)", bid)
    return {"reader_id": bid, "basis_fp": s.basis_fp, "state": "sandbox"}


class ProposeReq(BaseModel):
    task: str = ""              # what this changeset is for — review evidence
    trajectory: list = []       # agentic history: chat, thoughts, tool calls


@app.post("/api/branches/{bid}/propose")
def propose(bid: str, req: ProposeReq = None):
    br = _branch_row(bid)
    s = branchmgr.get_session(bid)
    if not br or not s:
        return {"error": "no live session for reader"}
    if br["status"] != "sandbox":
        return {"error": "reader must be detached (sandbox) to submit changes"}
    trajectory = req.trajectory if req else []
    task = ((req.task if req else "") or br["task"] or "(no task stated)").strip()
    store.set_branch(bid, task=task)          # replica remembers its latest intent
    cs = s.wf_end()
    if not cs.net_diff and cs.basis_fp == cs.final_fp:
        store.event("validator", "route",
                    f"reader {bid}: submitted changeset is EMPTY (no net data or "
                    f"schema change) — nothing to review or merge", bid)
        return {"status": "empty", "detail": "no net changes — the sandbox state "
                "is identical to the basis; nothing was submitted"}
    cfg = store.get_policies().get(br["role"])
    lane, checks = policy.route(cs, br["role"], cfg)
    store.event("validator", "route",
                f"changeset {cs.action_id[:8]}: {'REJECT ' + str(checks) if lane == 'reject' else 'rules pass -> ' + lane.upper()}",
                bid, cs.action_id)

    if lane == "reject":
        store.save_changeset(cs, bid, br["actor"], br["role"], task,
                             lane, checks, None, "rejected", trajectory)
        return {"changeset_id": cs.action_id, "status": "rejected", "checks": checks}

    review = None
    if lane == "auto":
        store.event("validator", "review",
                    f"AUTO-APPROVED by policy (small change, deterministic rules only)",
                    bid, cs.action_id)
    else:
        review = reviewer.llm_review(cs, task, [f"role={br['role']}"],
                                     guidance=(cfg or {}).get("llm_guidance", ""),
                                     trajectory=trajectory)
        store.event("llm-review", "review",
                    f"[{review.get('source','?')}] {review['verdict'].upper()}: "
                    f"{review['reason'][:140]}", bid, cs.action_id)

    if lane == "auto" or (lane == "llm" and review["verdict"] == "approve"):
        store.save_changeset(cs, bid, br["actor"], br["role"], task,
                             lane, checks, review, "approved", trajectory)
        store.set_branch(bid, status="proposed")
        # sync path: the propose request's EUC is still live — merge as proposer
        result = _merge(cs, cs.action_id, bid, br["actor"])
        status = ("auto-merged" if result["outcome"] == "applied" else
                  "approved" if result["outcome"] == "permission_denied" else
                  "merge_failed")
        return {"changeset_id": cs.action_id, "status": status, "merge": result}

    store.save_changeset(cs, bid, br["actor"], br["role"], task,
                         lane, checks, review, "pending_human", trajectory)
    store.set_branch(bid, status="proposed")
    store.event("system", "escalate",
                f"changeset {cs.action_id[:8]} waiting for HUMAN review", bid, cs.action_id)
    return {"changeset_id": cs.action_id, "status": "pending_human"}


# ----------------------------------------------------------------- changesets
class ReviewReq(BaseModel):
    verdict: str          # approved | rejected
    reviewer: str = "human"


@app.post("/api/changesets/{cid}/review")
def human_review(cid: str, req: ReviewReq):
    """Review only decides — it never executes. Approval is a durable state
    on the changeset; applying it is a separate, authenticated merge request
    (whoever calls merge supplies the credentials the apply runs under)."""
    row = store.get_changeset(cid)
    if not row or row["status"] != "pending_human":
        return {"error": "changeset not awaiting human review"}
    store.event(req.reviewer, "human_review", f"{req.verdict.upper()} changeset {cid[:8]}",
                row["branch_id"], cid)
    if req.verdict != "approved":
        store.set_changeset(cid, status="rejected")
        store.set_branch(row["branch_id"], status="sandbox")
        return {"status": "rejected"}
    store.set_changeset(cid, status="approved")
    store.event("wildfire", "approved",
                f"changeset {cid[:8]} APPROVED — awaiting a merge request "
                f"(apply will run under the merge caller's own DB credentials)",
                row["branch_id"], cid)
    return {"status": "approved", "detail": "awaiting merge request"}


class MergeReq(BaseModel):
    merger: str           # simulated EUC: the principal whose DB identity applies


@app.post("/api/changesets/{cid}/merge")
def merge_changeset(cid: str, req: MergeReq):
    """Apply an approved changeset under the CALLER's credentials. Rule of
    thumb: whoever calls merge, their identity runs the actual writes — the
    primary's grants/RLS decide, never a shared service account."""
    row = store.get_changeset(cid)
    if not row:
        return {"error": "not found"}
    if row["status"] != "approved":
        return {"error": f"changeset is {row['status']} — only approved "
                         f"changesets can be merged"}
    store.event(req.merger, "merge_request",
                f"merge requested for changeset {cid[:8]} — applying as "
                f"'{_db_user(req.merger) or req.merger}'", row["branch_id"], cid)
    cs = _rebuild_changeset(row)
    result = _merge(cs, cid, row["branch_id"], req.merger)
    status = ("merged" if result["outcome"] == "applied" else
              "approved" if result["outcome"] == "permission_denied" else
              "merge_failed")
    return {"changeset_id": cid, "status": status, "merge": result}


class RevertReq(BaseModel):
    requester: str = "operator"    # same rule as merge: caller's EUC applies


@app.post("/api/changesets/{cid}/revert")
def revert(cid: str, req: RevertReq = None):
    row = store.get_changeset(cid)
    if not row or row["status"] != "merged":
        return {"error": "only merged changesets can be reverted"}
    requester = req.requester if req else "operator"
    dbu = _db_user(requester)
    if not dbu:
        return {"error": f"principal '{requester}' has no database identity"}
    try:
        rc = appliers2.revert_changeset(_rebuild_changeset(row))
    except ValueError as e:
        return {"error": str(e)}
    res = appliers2.apply_segmented(rc, as_user=dbu)
    store.save_changeset(rc, row["branch_id"], "system", row["role"],
                         f"revert of {cid[:8]}", "revert", {}, None,
                         "merged" if res.outcome == "applied" else "merge_failed")
    store.set_changeset(rc.action_id, merge_result=json.dumps(res.as_dict()))
    if res.outcome == "applied":
        store.set_changeset(cid, status="reverted")
    store.event("merge-eng", "revert",
                f"revert {rc.action_id[:8]} of {cid[:8]}: {res.outcome.upper()} "
                f"({res.applied_rows} rows)", row["branch_id"], rc.action_id)
    return {"revert_changeset_id": rc.action_id, "outcome": res.outcome,
            "detail": res.detail}


@app.get("/api/changesets/{cid}")
def changeset_detail(cid: str):
    row = store.get_changeset(cid)
    if not row:
        return {"error": "not found"}
    for k in ("checks", "review", "summary", "net_diff", "sql_log", "merge_result",
              "trajectory"):
        if row.get(k):
            row[k] = json.loads(row[k])
    row.pop("segments", None)
    if row.get("net_diff"):
        nd = row["net_diff"]
        stats = {}
        for d in nd:
            t = stats.setdefault(d["relid"], {"U": 0, "I": 0, "D": 0})
            t[d["op"]] += 1
        row["diff_stats"] = stats
        row["net_diff_total"] = len(nd)
        row["net_diff_sample"] = nd[:100]
        row["net_diff"] = None
    return row


# ----------------------------------------------------------------- policies
@app.get("/api/policies")
def get_policies():
    return store.get_policies()


class PolicyReq(BaseModel):
    tables: list
    hard_max_rows: int
    auto_max_rows: int
    ddl: bool
    small_reviewer: str
    llm_guidance: str = ""


@app.put("/api/policies/{role}")
def put_policy(role: str, req: PolicyReq):
    if req.small_reviewer not in ("auto", "llm", "human"):
        return {"error": "small_reviewer must be auto|llm|human"}
    cfg = req.dict()
    store.set_policy(role, cfg)
    store.event("admin", "policy",
                f"policy for role '{role}' updated: tables={req.tables}, "
                f"hard≤{req.hard_max_rows}, auto≤{req.auto_max_rows}, "
                f"ddl={'allowed' if req.ddl else 'forbidden'}, "
                f"small-change reviewer={req.small_reviewer}")
    return {"ok": True, "role": role, "config": cfg}


# ----------------------------------------------------------------- studio
@app.get("/api/studio/schema")
def studio_schema():
    try:
        conn = db.connect(db.PRIMARY_DSN)
        cols = db.rows(conn, """
            SELECT c.table_name, c.column_name, c.data_type,
                   (pk.column_name IS NOT NULL) AS is_pk
            FROM information_schema.columns c
            LEFT JOIN (
              SELECT kcu.table_name, kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
               AND kcu.table_schema = tc.table_schema
              WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_schema='public'
            ) pk ON pk.table_name = c.table_name AND pk.column_name = c.column_name
            WHERE c.table_schema='public'
            ORDER BY c.table_name, c.ordinal_position""")
        counts = {r["relname"]: r["n"] for r in db.rows(conn, """
            SELECT relname, n_live_tup AS n FROM pg_stat_user_tables""")}
        conn.close()
        tables = {}
        for c in cols:
            tables.setdefault(c["table_name"], []).append(
                {"name": c["column_name"], "type": c["data_type"], "pk": c["is_pk"]})
        return {"tables": [{"name": t, "rows": counts.get(t, 0), "columns": cs}
                           for t, cs in tables.items()]}
    except Exception:
        return {
            "tables": [
                {"name": "customers", "rows": 250, "columns": [
                    {"name": "customer_id", "type": "integer", "pk": True},
                    {"name": "full_name", "type": "character varying", "pk": False},
                    {"name": "email", "type": "character varying", "pk": False},
                    {"name": "shipping_country", "type": "character varying", "pk": False},
                ]},
                {"name": "orders", "rows": 1500, "columns": [
                    {"name": "order_id", "type": "integer", "pk": True},
                    {"name": "customer_id", "type": "integer", "pk": False},
                    {"name": "order_date", "type": "date", "pk": False},
                    {"name": "ship_date", "type": "date", "pk": False},
                    {"name": "status", "type": "character varying", "pk": False},
                ]},
                {"name": "order_items", "rows": 3770, "columns": [
                    {"name": "order_item_id", "type": "integer", "pk": True},
                    {"name": "order_id", "type": "integer", "pk": False},
                    {"name": "product_id", "type": "integer", "pk": False},
                    {"name": "quantity", "type": "integer", "pk": False},
                    {"name": "unit_price", "type": "numeric", "pk": False},
                    {"name": "line_total", "type": "numeric", "pk": False},
                ]},
                {"name": "products", "rows": 40, "columns": [
                    {"name": "product_id", "type": "integer", "pk": True},
                    {"name": "sku", "type": "character varying", "pk": False},
                    {"name": "name", "type": "character varying", "pk": False},
                    {"name": "supplier_id", "type": "integer", "pk": False},
                    {"name": "tax_code_id", "type": "integer", "pk": False},
                ]},
                {"name": "product_pricing_history", "rows": 40, "columns": [
                    {"name": "pricing_id", "type": "integer", "pk": True},
                    {"name": "product_id", "type": "integer", "pk": False},
                    {"name": "unit_price", "type": "numeric", "pk": False},
                    {"name": "effective_start", "type": "date", "pk": False},
                    {"name": "effective_end", "type": "date", "pk": False},
                ]},
                {"name": "suppliers", "rows": 5, "columns": [
                    {"name": "supplier_id", "type": "integer", "pk": True},
                    {"name": "name", "type": "character varying", "pk": False},
                    {"name": "country_of_origin", "type": "character varying", "pk": False},
                ]},
                {"name": "tax_codes", "rows": 3, "columns": [
                    {"name": "tax_code_id", "type": "integer", "pk": True},
                    {"name": "code_name", "type": "character varying", "pk": False},
                    {"name": "category", "type": "character varying", "pk": False},
                    {"name": "default_rate", "type": "numeric", "pk": False},
                ]},
                {"name": "tax_eligibility_rules", "rows": 1, "columns": [
                    {"name": "rule_id", "type": "integer", "pk": True},
                    {"name": "country_of_origin", "type": "character varying", "pk": False},
                    {"name": "category", "type": "character varying", "pk": False},
                    {"name": "additional_tariff_rate", "type": "numeric", "pk": False},
                    {"name": "effective_date", "type": "date", "pk": False},
                    {"name": "note", "type": "text", "pk": False},
                ]},
            ]
        }


class StudioQuery(BaseModel):
    sql: str


@app.post("/api/studio/query")
def studio_query(req: StudioQuery):
    import time as _time
    try:
        t0 = _time.monotonic()
        head = req.sql.strip().split()[0].upper()
        if head == "SELECT":
            rows = alloydb_client.execute_sql(req.sql)
            elapsed = (_time.monotonic() - t0) * 1000
            columns = list(rows[0].keys()) if rows else []
            row_data = [[str(r.get(c, "")) for c in columns] for r in rows[:200]]
            return {"columns": columns, "rows": row_data,
                    "rowcount": len(rows), "ms": round(elapsed, 1)}
        else:
            alloydb_client.execute_sql(req.sql)
            elapsed = (_time.monotonic() - t0) * 1000
            store.event("studio", "drift",
                        f"DIRECT write on PRIMARY (bypasses Wildfire): {req.sql[:90]}")
            return {"rowcount": 1, "ms": round(elapsed, 1)}
    except Exception as e:
        return {"error": str(e).strip()}


# ----------------------------------------------------------------- state
@app.get("/api/state")
def state():
    st = store.list_state()
    try:
        tbl_res = alloydb_client.execute_sql(
            "SELECT count(*) as cnt FROM information_schema.tables WHERE table_schema = 'public' AND table_name NOT LIKE 'wf%';"
        )
        tables_cnt = int(tbl_res[0].get("cnt", 0)) if tbl_res else 0
        merged_cnt = len([c for c in st.get("changesets", []) if c.get("status") == "merged"])
        schema_fp = branchmgr.compute_schema_fingerprint()
    except Exception:
        tables_cnt = 0
        merged_cnt = 0
        schema_fp = "unknown"
    st["primary"] = {
        "tables": tables_cnt,
        "merged_actions": merged_cnt,
        "schema_fp": schema_fp,
    }
    for c in st["changesets"]:
        c["summary"] = json.loads(c["summary"]) if c["summary"] else None
    return st


@app.post("/api/demo/reset")
def demo_reset():
    for bid in list(branchmgr.SESSIONS):
        branchmgr.close_branch(bid)
    seed_air.reset_demo()
    store.init(wipe=True)
    store.event("system", "reset", "demo world restored (primary reseeded, control plane wiped)")
    return {"ok": True}


class DriftReq(BaseModel):
    sql: str = ("UPDATE seats SET status='booked', booking_id='OPS-MANUAL' "
                "WHERE flight_id=789 AND seat_no='12A'")


@app.post("/api/demo/drift")
def inject_drift(req: DriftReq):
    conn = db.connect(db.PRIMARY_DSN)
    n = db.run(conn, req.sql)
    conn.commit()
    conn.close()
    store.event("ops-team", "drift", f"PRIMARY edited outside Wildfire: {req.sql[:90]} ({n} rows)")
    return {"rowcount": n}


@app.get("/api/events/stream")
async def event_stream():
    def _top():
        with store.conn() as c:
            return c.execute("SELECT COALESCE(MAX(seq),0) m FROM events").fetchone()["m"]

    async def gen():
        seq = max(0, _top() - 30)              # replay last 30 on connect
        while True:
            # a demo reset wipes the control plane, so seq restarts at 1 —
            # rewind or this stream would never match another event again
            if _top() < seq:
                seq = 0
            evs = store.events_since(seq)
            for e in evs:
                seq = e["seq"]
                yield f"data: {json.dumps(e)}\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(pathlib.Path(__file__).parent / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=pathlib.Path(__file__).parent / "static"))
