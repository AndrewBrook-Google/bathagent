"""Capture v2: epoch-segmented ChangeSet supporting mixed DDL + DML.

Session model:
  wf_begin        -> basis snapshots (wf_base_*) + working snapshots (wf_snap_*)
                     + basis schema fingerprint
  execute(DML)    -> runs, logged; accumulates in the current epoch
  execute(DDL)    -> closes the current DML epoch (computes its incremental
                     diff), records the DDL as its own segment with
                     before/after schema fingerprints, rebuilds snapshots
  wf_end          -> closes the final epoch; also computes the NET diff
                     (wf_base_* vs final state) used as the merge contract
                     for M2 and for reporting

The segment list preserves the agent's original ordering, so both
"ADD COLUMN then backfill" (DDL before DML) and "backfill then SET NOT
NULL" (DML before DDL) replay correctly.
"""
import json
import uuid
import db

DDL_HEADS = {"CREATE", "ALTER", "DROP", "TRUNCATE"}
DML_HEADS = {"INSERT", "UPDATE", "DELETE"}


def schema_fingerprint(conn):
    """Stable hash of the public schema (columns, indexes, constraints).
    wf-prefixed harness tables are excluded."""
    return db.rows(conn, """
        SELECT md5(string_agg(x, '|' ORDER BY x)) AS fp FROM (
          SELECT table_name||'.'||column_name||':'||data_type||':'||is_nullable
                 ||':'||coalesce(column_default,'') AS x
          FROM information_schema.columns
          WHERE table_schema='public' AND table_name NOT LIKE 'wf%'
          UNION ALL
          SELECT 'idx:'||tablename||':'||indexdef
          FROM pg_indexes
          WHERE schemaname='public' AND tablename NOT LIKE 'wf%'
          UNION ALL
          SELECT 'con:'||conrelid::regclass::text||':'||conname||':'
                 ||pg_get_constraintdef(oid)
          FROM pg_constraint
          WHERE connamespace='public'::regnamespace
            AND conrelid::regclass::text NOT LIKE 'wf%'
        ) t""")[0]["fp"]


def user_tables(conn):
    return [r["tablename"] for r in db.rows(conn, """
        SELECT tablename FROM pg_tables
        WHERE schemaname='public' AND tablename NOT LIKE 'wf%'
        ORDER BY tablename""")]


def table_pk(conn, table):
    """Single-column PK or None (no-PK tables are refused at apply)."""
    pks = db.rows(conn, """
        SELECT a.attname FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary""", (table,))
    return pks[0]["attname"] if len(pks) == 1 else None


def table_columns(conn, table):
    return [r["column_name"] for r in db.rows(conn, """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position""", (table,))]


def compute_diff(conn, table, snap_table, pk):
    """Row diff snap vs current. Tolerates column-set mismatch (net diff
    across ALTERs): a missing key on the snap side reads as null."""
    cols = table_columns(conn, table)
    entries = db.rows(conn, f"""
        SELECT to_jsonb(s) AS before, to_jsonb(c) AS after
        FROM {snap_table} s FULL OUTER JOIN {table} c ON s.{pk} = c.{pk}
        WHERE to_jsonb(s) IS DISTINCT FROM to_jsonb(c)""")
    diff = []
    for e in entries:
        before, after = e["before"], e["after"]
        if before is None:
            op, pkval, changed = "I", after[pk], cols
        elif after is None:
            op, pkval, changed = "D", before[pk], cols
        else:
            op, pkval = "U", before[pk]
            changed = [c for c in cols if before.get(c) != after.get(c)]
            if not changed:      # only a column-set difference, no value change
                continue
        diff.append({"relid": table, "op": op, "pk": {pk: pkval},
                     "before": before, "after": after, "changed": changed})
    return diff


def all_rows_as_inserts(conn, table, pk):
    cols = table_columns(conn, table)
    return [{"relid": table, "op": "I", "pk": {pk: r["j"][pk]},
             "before": None, "after": r["j"], "changed": cols}
            for r in db.rows(conn, f"SELECT to_jsonb(t) AS j FROM {table} t")]


class ChangeSet2:
    def __init__(self, basis_fp, final_fp, segments, net_diff, sql_log):
        self.action_id = str(uuid.uuid4())
        self.basis_fp = basis_fp
        self.final_fp = final_fp
        self.segments = segments      # ordered: {kind: ddl|dml, ...}
        self.net_diff = net_diff      # merge contract for M2 / reporting
        self.sql_log = sql_log        # ordered statements incl. DDL (for M2)

    def summary(self):
        kinds = [s["kind"] for s in self.segments]
        ops = {}
        for d in self.net_diff:
            ops[d["op"]] = ops.get(d["op"], 0) + 1
        return {"segments": kinds, "statements": len(self.sql_log),
                "net_diff_rows": len(self.net_diff), "ops": ops,
                "ddl": self.basis_fp != self.final_fp,
                "diff_bytes": len(json.dumps(self.net_diff, default=str))}


class SandboxSession2:
    def __init__(self, dsn=None):
        self.conn = db.connect(dsn or db.SANDBOX_DSN)
        self.segments, self.sql_log = [], []
        self.basis_fp = None

    def wf_begin(self):
        self.basis_fp = schema_fingerprint(self.conn)
        with self.conn.cursor() as cur:
            for r in db.rows(self.conn, """SELECT tablename FROM pg_tables
                    WHERE schemaname='public' AND tablename LIKE 'wf_base%'
                       OR tablename LIKE 'wf_snap%'"""):
                cur.execute(f"DROP TABLE IF EXISTS {r['tablename']}")
            for t in user_tables(self.conn):
                cur.execute(f"CREATE TABLE wf_base_{t} AS SELECT * FROM {t}")
                cur.execute(f"CREATE TABLE wf_snap_{t} AS SELECT * FROM {t}")
        self.conn.commit()
        self.segments, self.sql_log = [], []

    def _rebuild_snaps(self):
        with self.conn.cursor() as cur:
            for r in db.rows(self.conn, """SELECT tablename FROM pg_tables
                    WHERE schemaname='public' AND tablename LIKE 'wf_snap%'"""):
                cur.execute(f"DROP TABLE {r['tablename']}")
            for t in user_tables(self.conn):
                cur.execute(f"CREATE TABLE wf_snap_{t} AS SELECT * FROM {t}")
        self.conn.commit()

    def _close_epoch(self):
        diff = []
        for t in user_tables(self.conn):
            snap = f"wf_snap_{t}"
            if not db.rows(self.conn, "SELECT 1 FROM pg_tables WHERE tablename=%s", (snap,)):
                continue
            pk = table_pk(self.conn, t)
            if pk is None:
                raise RuntimeError(f"table {t} has no single-column PK — refused")
            diff.extend(compute_diff(self.conn, t, snap, pk))
        if diff:
            self.segments.append({"kind": "dml", "diff": diff})
            self._rebuild_snaps()

    def execute(self, sql):
        head = sql.strip().split()[0].upper()
        if head in DDL_HEADS:
            self._close_epoch()
            fp_before = schema_fingerprint(self.conn)
            with self.conn.cursor() as cur:
                cur.execute(sql)
            self.conn.commit()
            fp_after = schema_fingerprint(self.conn)
            self.segments.append({"kind": "ddl", "statements": [sql],
                                  "fp_before": fp_before, "fp_after": fp_after})
            self._rebuild_snaps()
            self.sql_log.append({"sql": sql, "kind": "ddl"})
            return 0
        with self.conn.cursor() as cur:
            cur.execute(sql)
            n = cur.rowcount
        self.conn.commit()
        if head in DML_HEADS:
            self.sql_log.append({"sql": sql, "kind": "dml", "rows": n})
        return n

    def query(self, sql):
        return db.rows(self.conn, sql)

    def wf_end(self):
        self._close_epoch()
        net = []
        based = {r["tablename"][len("wf_base_"):] for r in db.rows(self.conn,
                 "SELECT tablename FROM pg_tables WHERE tablename LIKE 'wf_base%'")}
        for t in user_tables(self.conn):
            pk = table_pk(self.conn, t)
            if t in based:
                net.extend(compute_diff(self.conn, t, f"wf_base_{t}", pk))
            else:   # created during the session: every row is an insert
                net.extend(all_rows_as_inserts(self.conn, t, pk))
        return ChangeSet2(self.basis_fp, schema_fingerprint(self.conn),
                          self.segments, net, self.sql_log)

    def close(self):
        self.conn.close()
