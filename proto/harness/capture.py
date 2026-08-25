"""Capture layer: wf_begin / wf_end producing a ChangeSet {sql_log, diff}.

Diff strategy: snapshot-EXCEPT (RFC3 PoC path, zero PG config change).
wf_begin snapshots each watched table inside the sandbox; wf_end joins
snapshot vs current state on the PK and emits per-row
{pk, op, before, after, changed} entries.

The SQL log records every statement the agent session executes through
the harness — including exploratory noise — which is exactly what a
statement-level capture on a real system would see.
"""
import json
import db

WATCHED = {"orders": "id"}  # table -> pk column


class ChangeSet:
    def __init__(self, sql_log, diff):
        self.sql_log = sql_log      # list[str], writes only (DML)
        self.diff = diff            # list[dict], row-level changes

    def summary(self):
        ops = {}
        for d in self.diff:
            ops[d["op"]] = ops.get(d["op"], 0) + 1
        return {"statements": len(self.sql_log), "diff_rows": len(self.diff), "ops": ops,
                "diff_bytes": len(json.dumps(self.diff, default=str))}


class SandboxSession:
    """The agent's handle on the sandbox. Everything it runs goes through here."""

    def __init__(self):
        self.conn = db.connect(db.SANDBOX_DSN)
        self.sql_log = []
        self.recording = False

    def wf_begin(self):
        with self.conn.cursor() as cur:
            for table in WATCHED:
                cur.execute(f"DROP TABLE IF EXISTS wf_snap_{table}")
                cur.execute(f"CREATE TABLE wf_snap_{table} AS SELECT * FROM {table}")
        self.conn.commit()
        self.sql_log = []
        self.recording = True

    def execute(self, sql):
        """Agent runs a statement in the sandbox; DML gets logged."""
        with self.conn.cursor() as cur:
            cur.execute(sql)
            rowcount = cur.rowcount
        self.conn.commit()
        head = sql.strip().split()[0].upper()
        if self.recording and head in ("INSERT", "UPDATE", "DELETE"):
            self.sql_log.append(sql)
        return rowcount

    def query(self, sql):
        return db.rows(self.conn, sql)

    def wf_end(self):
        diff = []
        for table, pk in WATCHED.items():
            cols = [r["column_name"] for r in db.rows(self.conn, """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND table_schema = 'public'
                ORDER BY ordinal_position""", (table,))]
            entries = db.rows(self.conn, f"""
                SELECT COALESCE(to_jsonb(s), 'null') AS before,
                       COALESCE(to_jsonb(c), 'null') AS after
                FROM wf_snap_{table} s
                FULL OUTER JOIN {table} c ON s.{pk} = c.{pk}
                WHERE to_jsonb(s) IS DISTINCT FROM to_jsonb(c)""")
            for e in entries:
                before, after = e["before"], e["after"]
                if before is None:
                    op, pkval = "I", after[pk]
                    changed = cols
                elif after is None:
                    op, pkval = "D", before[pk]
                    changed = cols
                else:
                    op, pkval = "U", before[pk]
                    changed = [c for c in cols if before.get(c) != after.get(c)]
                diff.append({"relid": table, "op": op, "pk": {pk: pkval},
                             "before": before, "after": after, "changed": changed})
        self.recording = False
        return ChangeSet(list(self.sql_log), diff)

    def close(self):
        self.conn.close()
