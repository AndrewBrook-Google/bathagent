"""The two apply paths under one interface.

apply(changeset, approval_delay) -> ApplyResult

approval_delay simulates a slow (human/LLM) approval step. Where that wait
happens is the C5 story:
  - Option 1 must hold the primary transaction open across the wait
    (effect is only knowable after execution, pre-commit).
  - Option 2 waits BEFORE touching the primary; the diff is static.
"""
import json
import time
import psycopg2
import db


class ApplyResult:
    def __init__(self, option, outcome, applied_rows=0, conflicts=0,
                 lock_window_ms=0.0, detail=""):
        self.option = option
        self.outcome = outcome          # applied | conflict_abort | error
        self.applied_rows = applied_rows
        self.conflicts = conflicts
        self.lock_window_ms = lock_window_ms
        self.detail = detail

    def as_dict(self):
        return {"option": self.option, "outcome": self.outcome,
                "applied_rows": self.applied_rows, "conflicts": self.conflicts,
                "lock_window_ms": round(self.lock_window_ms, 1), "detail": self.detail}


def _column_types(conn, table):
    return {r["column_name"]: r["data_type"] for r in db.rows(conn, """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'""", (table,))}


# ---------------------------------------------------------------- Option 1
def apply_replay_sql(changeset, approval_delay=0.0):
    """BEGIN -> replay agent SQL -> (approval wait, locks held) -> COMMIT."""
    conn = db.connect(db.PRIMARY_DSN)
    t0 = time.monotonic()
    applied = 0
    try:
        with conn.cursor() as cur:
            for sql in changeset.sql_log:
                cur.execute(sql)
                applied += cur.rowcount
            # effect is now visible to the validator/approver — but only
            # inside this open transaction, so the wait holds locks
            time.sleep(approval_delay)
        conn.commit()
        lock_ms = (time.monotonic() - t0) * 1000
        return ApplyResult("option1", "applied", applied, 0, lock_ms)
    except psycopg2.Error as e:
        conn.rollback()
        lock_ms = (time.monotonic() - t0) * 1000
        return ApplyResult("option1", "error", 0, 0, lock_ms, str(e).strip())
    finally:
        conn.close()


# ---------------------------------------------------------------- Option 2
def apply_diff(changeset, approval_delay=0.0):
    """Offline approval -> BEGIN -> stage -> conflict check -> key-join apply -> COMMIT."""
    # the diff is static: the approver reads it with NO primary transaction open
    time.sleep(approval_delay)

    conn = db.connect(db.PRIMARY_DSN)
    t0 = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("""CREATE TEMP TABLE stg(
                relid text, op "char", pk jsonb, before jsonb, after jsonb,
                changed text[]) ON COMMIT DROP""")
            for d in changeset.diff:
                cur.execute("INSERT INTO stg VALUES (%s,%s,%s,%s,%s,%s)",
                            (d["relid"], d["op"], json.dumps(d["pk"], default=str),
                             json.dumps(d["before"], default=str),
                             json.dumps(d["after"], default=str), d["changed"]))

            tables = sorted({d["relid"] for d in changeset.diff})
            applied = 0
            for table in tables:
                types = _column_types(conn, table)
                pk = "id"

                # lock target rows in fixed order to avoid deadlocks
                cur.execute(f"""SELECT o.{pk} FROM {table} o
                    JOIN stg s ON s.relid = %s AND o.{pk} = (s.pk->>'{pk}')::bigint
                    ORDER BY o.{pk} FOR UPDATE""", (table,))

                # conflict check: before-image vs current, changed columns only
                cur.execute(f"""
                    SELECT count(*) FROM stg s
                    LEFT JOIN {table} o ON o.{pk} = (s.pk->>'{pk}')::bigint
                    WHERE s.relid = %s AND s.op <> 'I' AND (
                       o.{pk} IS NULL OR EXISTS (
                         SELECT 1 FROM unnest(s.changed) c
                         WHERE to_jsonb(o)->c IS DISTINCT FROM s.before->c))""",
                    (table,))
                conflicts = cur.fetchone()[0]
                if conflicts:
                    raise ConflictError(conflicts)

                # UPDATE: stamp after-values on, only for columns in `changed`
                changed_cols = sorted({c for d in changeset.diff
                                       if d["relid"] == table and d["op"] == "U"
                                       for c in d["changed"]})
                if changed_cols:
                    sets = ", ".join(
                        f"""{c} = CASE WHEN s.changed @> ARRAY['{c}']
                             THEN (s.after->>'{c}')::{types[c]} ELSE o.{c} END"""
                        for c in changed_cols)
                    cur.execute(f"""UPDATE {table} o SET {sets} FROM stg s
                        WHERE s.relid = %s AND s.op = 'U'
                          AND o.{pk} = (s.pk->>'{pk}')::bigint""", (table,))
                    applied += cur.rowcount

                # INSERT / DELETE paths
                cols = list(types)
                collist = ", ".join(cols)
                sellist = ", ".join(f"(s.after->>'{c}')::{types[c]}" for c in cols)
                cur.execute(f"""INSERT INTO {table} ({collist})
                    SELECT {sellist} FROM stg s
                    WHERE s.relid = %s AND s.op = 'I'""", (table,))
                applied += cur.rowcount
                cur.execute(f"""DELETE FROM {table} o USING stg s
                    WHERE s.relid = %s AND s.op = 'D'
                      AND o.{pk} = (s.pk->>'{pk}')::bigint""", (table,))
                applied += cur.rowcount

        conn.commit()
        lock_ms = (time.monotonic() - t0) * 1000
        return ApplyResult("option2", "applied", applied, 0, lock_ms)
    except ConflictError as e:
        conn.rollback()
        lock_ms = (time.monotonic() - t0) * 1000
        return ApplyResult("option2", "conflict_abort", 0, e.count, lock_ms,
                           f"{e.count} row(s) drifted since capture")
    except psycopg2.Error as e:
        conn.rollback()
        lock_ms = (time.monotonic() - t0) * 1000
        return ApplyResult("option2", "error", 0, 0, lock_ms, str(e).strip())
    finally:
        conn.close()


class ConflictError(Exception):
    def __init__(self, count):
        self.count = count


def undo_diff(changeset, approval_delay=0.0):
    """Undo = the same apply path with before/after swapped."""
    import copy
    inv = copy.deepcopy(changeset)
    for d in inv.diff:
        d["before"], d["after"] = d["after"], d["before"]
        d["op"] = {"I": "D", "D": "I", "U": "U"}[d["op"]]
    return apply_diff(inv, approval_delay)
