"""Merge engine v2: two apply paths for mixed DDL+DML changesets.

M1 apply_segmented   — one txn, segments replayed in captured order:
                       DDL segment  = fingerprint check -> replay -> re-check
                       DML segment  = stage -> lock -> before-image check -> apply
M2 apply_execute_verify — one txn: replay the full sql_log, recompute the
                       net diff on the primary, strict-compare against the
                       approved contract (before AND after images), compare
                       final schema fingerprint; mismatch -> ROLLBACK.

Outcomes: applied | schema_drift | data_conflict | verify_mismatch | error
Both paths start with the basis fingerprint guard and the wf_applied
idempotency gate; approval latency is spent BEFORE the transaction opens.
"""
import json
import time
import psycopg2
import db
import capture2


class MergeResult:
    def __init__(self, mode, outcome, applied_rows=0, lock_window_ms=0.0, detail=""):
        self.mode, self.outcome = mode, outcome
        self.applied_rows, self.lock_window_ms = applied_rows, lock_window_ms
        self.detail = detail

    def as_dict(self):
        return {"mode": self.mode, "outcome": self.outcome,
                "applied_rows": self.applied_rows,
                "lock_window_ms": round(self.lock_window_ms, 1), "detail": self.detail}


class DataConflict(Exception):
    def __init__(self, count, table):
        self.count, self.table = count, table


class SchemaDrift(Exception):
    def __init__(self, where):
        self.where = where


class VerifyMismatch(Exception):
    def __init__(self, detail):
        self.detail = detail


def _column_types(conn, table):
    return {r["column_name"]: r["data_type"] for r in db.rows(conn, """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name=%s AND table_schema='public'""", (table,))}


def _apply_dml_segment(conn, cur, diff, seg_no):
    stg = f"stg_{seg_no}"
    cur.execute(f"""CREATE TEMP TABLE {stg}(relid text, op "char", pk jsonb,
        before jsonb, after jsonb, changed text[]) ON COMMIT DROP""")
    for d in diff:
        cur.execute(f"INSERT INTO {stg} VALUES (%s,%s,%s,%s,%s,%s)",
                    (d["relid"], d["op"], json.dumps(d["pk"], default=str),
                     json.dumps(d["before"], default=str),
                     json.dumps(d["after"], default=str), d["changed"]))
    applied = 0
    for table in sorted({d["relid"] for d in diff}):
        pk = capture2.table_pk(conn, table)
        if pk is None:
            raise RuntimeError(f"{table}: no single-column PK — refused")
        types = _column_types(conn, table)

        cur.execute(f"""SELECT o.{pk} FROM {table} o
            JOIN {stg} s ON s.relid=%s AND o.{pk} = (s.pk->>'{pk}')::{types[pk]}
            ORDER BY o.{pk} FOR UPDATE""", (table,))

        cur.execute(f"""SELECT count(*) FROM {stg} s
            LEFT JOIN {table} o ON o.{pk} = (s.pk->>'{pk}')::{types[pk]}
            WHERE s.relid=%s AND s.op <> 'I' AND (
              o.{pk} IS NULL OR EXISTS (
                SELECT 1 FROM unnest(s.changed) c
                WHERE to_jsonb(o)->c IS DISTINCT FROM s.before->c))""", (table,))
        n = cur.fetchone()[0]
        if n:
            raise DataConflict(n, table)

        changed_cols = sorted({c for d in diff if d["relid"] == table
                               and d["op"] == "U" for c in d["changed"]})
        if changed_cols:
            sets = ", ".join(
                f"""{c} = CASE WHEN s.changed @> ARRAY['{c}']
                     THEN (s.after->>'{c}')::{types[c]} ELSE o.{c} END"""
                for c in changed_cols)
            cur.execute(f"""UPDATE {table} o SET {sets} FROM {stg} s
                WHERE s.relid=%s AND s.op='U'
                  AND o.{pk} = (s.pk->>'{pk}')::{types[pk]}""", (table,))
            applied += cur.rowcount

        cols = list(types)
        cur.execute(f"""INSERT INTO {table} ({', '.join(cols)})
            SELECT {', '.join(f"(s.after->>'{c}')::{types[c]}" for c in cols)}
            FROM {stg} s WHERE s.relid=%s AND s.op='I'""", (table,))
        applied += cur.rowcount
        cur.execute(f"""DELETE FROM {table} o USING {stg} s
            WHERE s.relid=%s AND s.op='D'
              AND o.{pk} = (s.pk->>'{pk}')::{types[pk]}""", (table,))
        applied += cur.rowcount
    return applied


MERGE_LOCK_KEY = 0x5C00F  # global merge queue: one merge txn at a time

def _guards(conn, cur, cs):
    # serialize merges: eliminates fingerprint TOCTOU between a DDL merge
    # committing while another merge is mid-flight; released at COMMIT/ROLLBACK
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (MERGE_LOCK_KEY,))
    fp = capture2.schema_fingerprint(conn)
    if fp != cs.basis_fp:
        raise SchemaDrift("basis")
    cur.execute("INSERT INTO wf_applied(action_id) VALUES (%s)", (cs.action_id,))


def apply_segmented(cs, approval_delay=0.0, as_user=None):
    """as_user: the merge caller's DB identity (simulated IAM EUC). The apply
    transaction runs under THAT user's grants/ownership/RLS — the primary
    itself is the enforcement boundary, never the sandbox."""
    time.sleep(approval_delay)          # offline approval, primary untouched
    conn = db.connect(db.primary_dsn_as(as_user) if as_user else db.PRIMARY_DSN)
    t0 = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            _guards(conn, cur, cs)
            applied = 0
            for i, seg in enumerate(cs.segments):
                if seg["kind"] == "ddl":
                    if capture2.schema_fingerprint(conn) != seg["fp_before"]:
                        raise SchemaDrift(f"segment {i} pre-DDL")
                    for st in seg["statements"]:
                        cur.execute(st)
                    if capture2.schema_fingerprint(conn) != seg["fp_after"]:
                        raise SchemaDrift(f"segment {i} post-DDL")
                else:
                    applied += _apply_dml_segment(conn, cur, seg["diff"], i)
        conn.commit()
        return MergeResult("M1_segmented", "applied", applied,
                           (time.monotonic() - t0) * 1000)
    except SchemaDrift as e:
        conn.rollback()
        return MergeResult("M1_segmented", "schema_drift", 0,
                           (time.monotonic() - t0) * 1000, f"fingerprint mismatch at {e.where}")
    except DataConflict as e:
        conn.rollback()
        return MergeResult("M1_segmented", "data_conflict", 0,
                           (time.monotonic() - t0) * 1000,
                           f"{e.count} drifted row(s) in {e.table}")
    except psycopg2.errors.InsufficientPrivilege as e:
        conn.rollback()
        return MergeResult("M1_segmented", "permission_denied", 0,
                           (time.monotonic() - t0) * 1000,
                           f"primary DB refused for '{as_user or 'postgres'}': "
                           + str(e).strip()[:160])
    except psycopg2.Error as e:
        conn.rollback()
        return MergeResult("M1_segmented", "error", 0,
                           (time.monotonic() - t0) * 1000, str(e).strip()[:200])
    finally:
        conn.close()


def revert_changeset(cs):
    """Best-effort revert: a NEW changeset with before/after swapped, applied
    through the same engine (same OCC guards). DML-only — DDL cannot be
    mechanically inverted; refuse so the caller escalates instead."""
    import copy
    if cs.basis_fp != cs.final_fp:
        raise ValueError("changeset contains DDL — no mechanical revert")
    inv = copy.deepcopy(cs.net_diff)
    for d in inv:
        d["before"], d["after"] = d["after"], d["before"]
        d["op"] = {"I": "D", "D": "I", "U": "U"}[d["op"]]
    rc = capture2.ChangeSet2(cs.final_fp, cs.basis_fp,
                             [{"kind": "dml", "diff": inv}], inv, [])
    rc.reverts = cs.action_id            # audit chain
    return rc


def _canon(diff):
    out = set()
    for d in diff:
        before, after = d["before"] or {}, d["after"] or {}
        out.add((d["relid"], json.dumps(d["pk"], sort_keys=True, default=str), d["op"],
                 json.dumps({c: after.get(c) for c in d["changed"]},
                            sort_keys=True, default=str),
                 json.dumps({c: before.get(c) for c in d["changed"]},
                            sort_keys=True, default=str)))
    return out


def apply_execute_verify(cs, approval_delay=0.0, as_user=None):
    time.sleep(approval_delay)
    conn = db.connect(db.primary_dsn_as(as_user) if as_user else db.PRIMARY_DSN)
    t0 = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            _guards(conn, cur, cs)

            touched = sorted({d["relid"] for d in cs.net_diff})
            existing = set(capture2.user_tables(conn))
            for t in touched:
                if t in existing:
                    cur.execute(f"CREATE TEMP TABLE ev_snap_{t} ON COMMIT DROP AS SELECT * FROM {t}")

            for entry in cs.sql_log:            # replay everything, in order
                cur.execute(entry["sql"])

            replay_diff = []
            for t in touched:
                pk = capture2.table_pk(conn, t)
                if t in existing:
                    replay_diff.extend(capture2.compute_diff(conn, t, f"ev_snap_{t}", pk))
                else:
                    replay_diff.extend(capture2.all_rows_as_inserts(conn, t, pk))

            approved, actual = _canon(cs.net_diff), _canon(replay_diff)
            if approved != actual:
                sample = list(approved.symmetric_difference(actual))[:3]
                raise VerifyMismatch(
                    f"{len(approved - actual)} approved-only / "
                    f"{len(actual - approved)} replay-only rows; e.g. {sample[0][:3]}")
            if capture2.schema_fingerprint(conn) != cs.final_fp:
                raise VerifyMismatch("final schema fingerprint differs")
        conn.commit()
        return MergeResult("M2_exec_verify", "applied", len(cs.net_diff),
                           (time.monotonic() - t0) * 1000)
    except SchemaDrift as e:
        conn.rollback()
        return MergeResult("M2_exec_verify", "schema_drift", 0,
                           (time.monotonic() - t0) * 1000, f"fingerprint mismatch at {e.where}")
    except VerifyMismatch as e:
        conn.rollback()
        return MergeResult("M2_exec_verify", "verify_mismatch", 0,
                           (time.monotonic() - t0) * 1000, str(e.detail)[:300])
    except psycopg2.errors.InsufficientPrivilege as e:
        conn.rollback()
        return MergeResult("M2_exec_verify", "permission_denied", 0,
                           (time.monotonic() - t0) * 1000,
                           f"primary DB refused for '{as_user or 'postgres'}': "
                           + str(e).strip()[:160])
    except psycopg2.Error as e:
        conn.rollback()
        return MergeResult("M2_exec_verify", "error", 0,
                           (time.monotonic() - t0) * 1000, str(e).strip()[:200])
    finally:
        conn.close()
