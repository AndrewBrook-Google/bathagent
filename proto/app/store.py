"""Control-plane metadata store (SQLite, WAL). One file = one demo world."""
import json
import pathlib
import sqlite3
import time
import uuid

DB_PATH = pathlib.Path(__file__).parent / "scoop_ctl.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches(
  id TEXT PRIMARY KEY, actor TEXT, role TEXT, task TEXT,
  status TEXT,           -- isolated | proposed | merged | abandoned
  basis_fp TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS changesets(
  id TEXT PRIMARY KEY, branch_id TEXT, actor TEXT, role TEXT, task TEXT,
  status TEXT,           -- pending_human | approved | rejected | merged |
                         -- merge_failed | reverted
  lane TEXT, checks TEXT, review TEXT, summary TEXT,
  net_diff TEXT, sql_log TEXT, segments TEXT,
  basis_fp TEXT, final_fp TEXT,
  merge_result TEXT, reverts TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, kind TEXT,
  branch_id TEXT, changeset_id TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS policies(
  role TEXT PRIMARY KEY, config TEXT, updated_at REAL);
"""

DEFAULT_POLICIES = {
    "bathagent": {"tables": ["orders", "order_items", "customers"], "hard_max_rows": 50,
                  "auto_max_rows": 20, "ddl": False, "small_reviewer": "llm",
                  "llm_guidance":
                  "Approve routine customer orders, item quantity adjustments, "
                  "and customer record updates. Escalate unexpected price discounts > 25%, "
                  "bulk deletions, or edits to unrelated tables."},
    "booking":   {"tables": ["orders", "order_items", "customers"], "hard_max_rows": 50,
                  "auto_max_rows": 20, "ddl": False, "small_reviewer": "llm",
                  "llm_guidance":
                  "Approve routine customer orders, item quantity adjustments, "
                  "and customer record updates. Escalate unexpected price discounts > 25%, "
                  "bulk deletions, or edits to unrelated tables."},
    "developer": {"tables": ["orders", "order_items", "products", "product_pricing_history", "suppliers", "tax_codes", "tax_eligibility_rules"],
                  "hard_max_rows": 10000, "auto_max_rows": 100, "ddl": True,
                  "small_reviewer": "llm",
                  "llm_guidance":
                  "This role ships schema and catalog/tax changes; DDL always goes to a human "
                  "— your job is a pre-screen. Summarize risk for the approver: table "
                  "rewrites, long locks, tariff rate adjustments, or large backfills."},
    "analytics": {"tables": ["orders", "products", "suppliers"], "hard_max_rows": 5000,
                  "auto_max_rows": 100, "ddl": False, "small_reviewer": "llm",
                  "llm_guidance":
                  "Approve data-quality and inventory sync changes that implement the stated "
                  "task exactly: supplier catalog updates, stock adjustments, only the named columns."},
}


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init(wipe=False):
    if wipe and DB_PATH.exists():
        DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            p = pathlib.Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    with conn() as c:
        c.executescript(SCHEMA)
        try:
            c.execute("ALTER TABLE changesets ADD COLUMN trajectory TEXT")
        except sqlite3.OperationalError:
            pass                      # column already exists
        for role, cfg in DEFAULT_POLICIES.items():
            c.execute("INSERT OR REPLACE INTO policies VALUES(?,?,?)",
                      (role, json.dumps(cfg), time.time()))


def get_policies():
    with conn() as c:
        stored = {r["role"]: json.loads(r["config"])
                  for r in c.execute("SELECT * FROM policies ORDER BY role ASC")}
    # merge defaults for fields added after a row was written (e.g. llm_guidance)
    all_roles = sorted(set(DEFAULT_POLICIES.keys()) | set(stored.keys()))
    return {role: {**DEFAULT_POLICIES.get(role, {}), **stored.get(role, {})}
            for role in all_roles}



def set_policy(role, cfg):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO policies VALUES(?,?,?)",
                  (role, json.dumps(cfg), time.time()))


def event(actor, kind, message, branch_id=None, changeset_id=None):
    with conn() as c:
        c.execute("INSERT INTO events(ts,actor,kind,branch_id,changeset_id,message)"
                  " VALUES(?,?,?,?,?,?)",
                  (time.time(), actor, kind, branch_id, changeset_id, message))


def events_since(seq):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT 200", (seq,))]


def _slug(name):
    s = "".join(c if c.isalnum() else "-" for c in (name or "").lower()).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:24]


def new_branch(actor, role, task, basis_fp, name=None):
    """Replica id: rd-<operator name> when given, else rd-<random>."""
    slug = _slug(name)
    bid = "rd-" + (slug or uuid.uuid4().hex[:8])
    with conn() as c:
        if c.execute("SELECT 1 FROM branches WHERE id=?", (bid,)).fetchone():
            bid += "-" + uuid.uuid4().hex[:4]
    now = time.time()
    with conn() as c:
        c.execute("INSERT INTO branches VALUES(?,?,?,?,?,?,?,?)",
                  (bid, actor, role, task, "isolated", basis_fp, now, now))
    return bid


def set_branch(bid, **kw):
    kw["updated_at"] = time.time()
    with conn() as c:
        c.execute(f"UPDATE branches SET {','.join(k + '=?' for k in kw)} WHERE id=?",
                  (*kw.values(), bid))


def save_changeset(cs, branch_id, actor, role, task, lane, checks, review, status,
                   trajectory=None):
    now = time.time()
    with conn() as c:
        c.execute("""INSERT INTO changesets(id,branch_id,actor,role,task,status,lane,
            checks,review,summary,net_diff,sql_log,segments,basis_fp,final_fp,
            reverts,created_at,updated_at,trajectory)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cs.action_id, branch_id, actor, role, task, status, lane,
             json.dumps(checks, default=str), json.dumps(review),
             json.dumps(cs.summary(), default=str),
             json.dumps(cs.net_diff, default=str),
             json.dumps(cs.sql_log, default=str),
             json.dumps(cs.segments, default=str),
             cs.basis_fp, cs.final_fp,
             getattr(cs, "reverts", None), now, now,
             json.dumps(trajectory or [], default=str)))


def set_changeset(cid, **kw):
    kw["updated_at"] = time.time()
    with conn() as c:
        c.execute(f"UPDATE changesets SET {','.join(k + '=?' for k in kw)} WHERE id=?",
                  (*kw.values(), cid))


def get_changeset(cid):
    with conn() as c:
        r = c.execute("SELECT * FROM changesets WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None


def list_state():
    with conn() as c:
        return {
            "branches": [dict(r) for r in c.execute(
                "SELECT * FROM branches ORDER BY created_at DESC LIMIT 50")],
            "changesets": [dict(r) for r in c.execute(
                "SELECT id,branch_id,actor,role,task,status,lane,summary,reverts,"
                "created_at FROM changesets ORDER BY created_at DESC LIMIT 50")],
        }
