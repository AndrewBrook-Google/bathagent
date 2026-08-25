"""Prod-PoC apply side, v3: three-phase apply for mixed DDL+DML changesets.

The original SQL order is gone (never captured). Ordering is reconstructed
from the delta's own dependency structure:

  phase R (relax)    — DDL that makes the DML phase's writes valid:
                       CREATE TABLE, ADD COLUMN (nullable here), DROP COLUMN,
                       DROP NOT NULL, DEFAULT changes, DROP TABLE
  phase M (DML)      — staged, op-aware-OCC keyed stamps:
                       INSERTs parent-first, UPDATEs, DELETEs child-first
  phase D (restrict) — DDL that validates what the DML produced:
                       SET NOT NULL (and, beyond PoC: ADD CONSTRAINT,
                       CREATE INDEX, type narrowing)

One transaction; every failure is a full rollback.

Every identifier is quoted: table and column names come from the sandbox and
may be mixed-case, reserved words, or contain quotes. Whole-row references are
written alias.* — a column named like the alias would otherwise shadow it.
Relations and rowtypes are schema-qualified via qt(), because pg_catalog wins
an unqualified lookup (a table named 'line' or 'pg_class' would otherwise be
read as the built-in type or the catalog).
"""
import collections
import time

import psycopg2
import psycopg2.extras

import wf_json
from wf_capture import (rows, run, pk_cols, col_types, table_fingerprint,
                        server_written, pin_gucs, SchemaChanged, q, qt, sq)

json = wf_json


class SchemaDrift(Exception):
    pass


class DataConflict(Exception):
    pass


# --------------------------------------------------------------- DDL phases
def ddl_phases(schema_delta):
    """Whitelisted ops -> (relax_statements, restrict_statements)."""
    relax, restrict = [], []
    for o in schema_delta:
        t = qt(o["table"])
        if o["op"] == "create_table":
            cols = []
            for c in o["order"]:
                d = o["columns"][c]
                s = f"{q(c)} {d['type']}"
                if d.get("collation"):
                    s += f" COLLATE {q(d['collation'])}"
                if d.get("generated"):
                    # for a generated column 'default' holds the GENERATION
                    # expression; emitting it as DEFAULT is both wrong and
                    # rejected ("cannot use column reference in DEFAULT")
                    s += f" GENERATED ALWAYS AS ({d['default']}) STORED"
                elif d.get("identity"):
                    s += (" GENERATED " + ("ALWAYS" if d["identity"] == "a"
                                           else "BY DEFAULT") + " AS IDENTITY")
                elif d["default"]:
                    s += f" DEFAULT {d['default']}"
                if d["notnull"]:
                    s += " NOT NULL"
                cols.append(s)
            if o["pk"]:
                cols.append(f"PRIMARY KEY ({', '.join(q(c) for c in o['pk'])})")
            relax.append(f"CREATE TABLE {t} ({', '.join(cols)})")
        elif o["op"] == "drop_table":
            relax.append(f"DROP TABLE {t}")
        elif o["op"] == "add_column":
            relax.append(f"ALTER TABLE {t} ADD COLUMN {q(o['col'])} {o['type']}"
                         + (f" DEFAULT {o['default']}" if o["default"] else ""))
        elif o["op"] == "drop_column":
            relax.append(f"ALTER TABLE {t} DROP COLUMN {q(o['col'])}")
        elif o["op"] == "drop_not_null":
            relax.append(f"ALTER TABLE {t} ALTER COLUMN {q(o['col'])} "
                         f"DROP NOT NULL")
        elif o["op"] == "set_default":
            relax.append(f"ALTER TABLE {t} ALTER COLUMN {q(o['col'])} "
                         + (f"SET DEFAULT {o['default']}" if o["default"]
                            else "DROP DEFAULT"))
        elif o["op"] == "set_not_null":
            restrict.append(f"ALTER TABLE {t} ALTER COLUMN {q(o['col'])} "
                            f"SET NOT NULL")
        else:
            raise SchemaChanged(f"op '{o['op']}' has no generator")
    return relax, restrict


# ---------------------------------------------------------------- FK topology
def fk_topo_order(conn, tables):
    edges = rows(conn, """
        SELECT c.relname AS child, p.relname AS parent
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_class p ON p.oid = k.confrelid
        JOIN pg_namespace cn ON cn.oid = c.relnamespace
        JOIN pg_namespace pn ON pn.oid = p.relnamespace
        WHERE k.contype = 'f'
          AND cn.nspname = 'public' AND pn.nspname = 'public'""")
    deps = {t: set() for t in tables}
    for e in edges:
        if e["child"] in deps and e["parent"] in deps and e["child"] != e["parent"]:
            deps[e["child"]].add(e["parent"])
    order = []
    while deps:
        free = sorted(t for t, d in deps.items() if not d)
        if not free:
            raise RuntimeError(f"FK cycle among {sorted(deps)} — out of scope")
        for t in free:
            order.append(t)
            deps.pop(t)
        for d in deps.values():
            d.difference_update(free)
    return order


# ---------------------------------------------------------------- literal SQL
def literal_sql(conn, changeset):
    """Review rendering: DDL phases + one plain statement per row.
    Rendered AGAINST THE SANDBOX (has the final schema for new columns)."""
    relax, restrict = ddl_phases(changeset["schema_delta"])
    delta = changeset["data_delta"]
    out = [f"-- phase R (relax) --"] + [s + ";" for s in relax]
    out.append("-- phase M (data) --")
    with conn.cursor() as cur:
        types_cache = {}

        def lit(table, col, val):
            t = types_cache.setdefault(table, col_types(conn, table))[col]
            return "NULL" if val is None else cur.mogrify(f"%s::{t}", (val,)).decode()

        tables_in = sorted({d["relid"] for d in delta})
        order = fk_topo_order(conn, tables_in)
        sw = {t: server_written(conn, t) for t in tables_in}
        for d in sorted((x for x in delta if x["op"] == "I"),
                        key=lambda d: (order.index(d["relid"]), str(d["pk"]))):
            gen, ident = sw[d["relid"]]
            cols = sorted(set(d["after"]) - gen)
            out.append(f"INSERT INTO {qt(d['relid'])} "
                       f"({', '.join(q(c) for c in cols)})"
                       + (" OVERRIDING SYSTEM VALUE" if ident else "") + " VALUES "
                       f"({', '.join(lit(d['relid'], c, d['after'][c]) for c in cols)});")
        for d in (x for x in delta if x["op"] == "U"):
            sets = ", ".join(f"{q(c)} = {lit(d['relid'], c, d['after'].get(c))}"
                             for c in d["changed"] if c not in sw[d["relid"]][0])
            where = " AND ".join(f"{q(k)} = {lit(d['relid'], k, v)}"
                                 for k, v in d["pk"].items())
            out.append(f"UPDATE {qt(d['relid'])} SET {sets} WHERE {where};")
        for d in sorted((x for x in delta if x["op"] == "D"),
                        key=lambda d: (-order.index(d["relid"]), str(d["pk"]))):
            where = " AND ".join(f"{q(k)} = {lit(d['relid'], k, v)}"
                                 for k, v in d["pk"].items())
            out.append(f"DELETE FROM {qt(d['relid'])} WHERE {where};")
    out += ["-- phase D (restrict) --"] + [s + ";" for s in restrict]
    return out


# ---------------------------------------------------------------- staged apply
MERGE_LOCK_KEY = 0x5C00F


class ApplyResult:
    def __init__(self, outcome, applied_rows=0, ms=0.0, detail=""):
        self.outcome, self.applied_rows = outcome, applied_rows
        self.ms, self.detail = ms, detail

    def __repr__(self):
        return (f"[{self.outcome}] rows={self.applied_rows} "
                f"lock_window={self.ms:.1f}ms {self.detail}")


def apply_staged(conn, changeset, action_id):
    delta = changeset["data_delta"]
    relax, restrict = ddl_phases(changeset["schema_delta"])
    new_tables = {o["table"] for o in changeset["schema_delta"]
                  if o["op"] == "create_table"}
    dropped = {o["table"] for o in changeset["schema_delta"]
               if o["op"] == "drop_table"}
    tables = sorted({d["relid"] for d in delta})
    t0 = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            pin_gucs(cur)      # same value rendering as the capture txn
            # deferrable constraints check at COMMIT: lets unique-value swaps
            # and FK cycles through when the schema declares them DEFERRABLE
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (MERGE_LOCK_KEY,))
            cur.execute("""CREATE TABLE IF NOT EXISTS wf_applied(
                action_id text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now())""")
            cur.execute("INSERT INTO wf_applied(action_id) VALUES (%s)", (action_id,))

            # checkpoint #2 — primary must still look like the basis:
            # touched existing tables unchanged, tables-to-create absent
            for t, fp in changeset["basis_fp"].items():
                if t in dropped:
                    continue
                if table_fingerprint(conn, t) != fp:
                    raise SchemaDrift(f"table '{t}' structure drifted since basis")
            for t in new_tables:
                cur.execute("SELECT to_regclass(%s)", (qt(t),))
                if cur.fetchone()[0] is not None:
                    raise SchemaDrift(f"table '{t}' already exists on primary")

            for stmt in relax:                                   # phase R
                cur.execute(stmt)

            # ---------------- phase M (types/topology read POST-relax) ------
            cur.execute("""CREATE TEMP TABLE stg(relid text, op "char", pk jsonb,
                before jsonb, after jsonb, changed text[]) ON COMMIT DROP""")
            psycopg2.extras.execute_values(
                cur, "INSERT INTO stg VALUES %s",
                [(d["relid"], d["op"], json.dumps(d["pk"]),
                  json.dumps(d["before"]), json.dumps(d["after"]), d["changed"])
                 for d in delta])
            order = fk_topo_order(conn, tables)
            applied = 0

            for t in sorted(tables):
                pks, types = pk_cols(conn, t), col_types(conn, t)
                if not pks:
                    raise SchemaDrift(f"table '{t}' has no primary key on the "
                                      f"primary — its rows cannot be keyed")
                join = " AND ".join(f"(s.pk->>{sq(c)})::{types[c]} = _wfc.{q(c)}"
                                    for c in pks)
                pk0 = q(pks[0])
                cur.execute(f"""SELECT _wfc.* FROM {qt(t)} _wfc JOIN stg s ON {join}
                    WHERE s.relid = %s ORDER BY _wfc.{pk0} FOR UPDATE""", (t,))
                # op-aware OCC; COALESCE: a column that did not exist at
                # journal time reads as null, matching a freshly added column
                cur.execute(f"""
                    SELECT count(*) FROM stg s LEFT JOIN {qt(t)} _wfc ON {join}
                    WHERE s.relid = %s AND (
                      (s.op = 'I' AND _wfc.{pk0} IS NOT NULL)
                      OR (s.op <> 'I' AND (_wfc.{pk0} IS NULL
                          OR EXISTS (SELECT 1 FROM unnest(s.changed) AS _wfk(cn)
                                     WHERE COALESCE(to_jsonb(_wfc.*)->_wfk.cn,'null'::jsonb)
                                           IS DISTINCT FROM
                                           COALESCE(s.before->_wfk.cn,'null'::jsonb)))))""",
                    (t,))
                n = cur.fetchone()[0]
                if n:
                    raise DataConflict(n, t)

            for t in order:                                       # parent-first
                if not any(d["relid"] == t and d["op"] == "I" for d in delta):
                    continue
                # jsonb_populate_record: PG's own jsonb->rowtype conversion —
                # handles arrays, bytea, jsonb columns, numerics natively
                # (the text round-trip (after->>'col')::type breaks on arrays).
                # Explicit column list, not r.*: a STORED generated column
                # rejects any value, and an ALWAYS identity column needs the
                # override to keep the sandbox's keys (FKs point at them).
                gen, ident = server_written(conn, t)
                ins = [c for c in col_types(conn, t) if c not in gen]
                ov = " OVERRIDING SYSTEM VALUE" if ident else ""
                cur.execute(f"""INSERT INTO {qt(t)}
                    ({', '.join(q(c) for c in ins)}){ov}
                    SELECT {', '.join('r.' + q(c) for c in ins)} FROM stg s,
                         jsonb_populate_record(NULL::{qt(t)}, s.after) r
                    WHERE s.relid = %s AND s.op = 'I'""", (t,))
                applied += cur.rowcount

            for t in sorted(tables):
                gen, _ = server_written(conn, t)
                changed_cols = sorted({c for d in delta
                                       if d["relid"] == t and d["op"] == "U"
                                       for c in d["changed"]} - gen)
                if not changed_cols:
                    continue
                pks, types = pk_cols(conn, t), col_types(conn, t)
                join = " AND ".join(f"(s.pk->>{sq(c)})::{types[c]} = _wfc.{q(c)}"
                                    for c in pks)
                sets = ", ".join(
                    f"""{q(c)} = CASE WHEN s.changed @> ARRAY[{sq(c)}]
                         THEN r.{q(c)} ELSE _wfc.{q(c)} END"""
                    for c in changed_cols)
                cur.execute(f"""UPDATE {qt(t)} _wfc SET {sets}
                    FROM stg s, jsonb_populate_record(NULL::{qt(t)}, s.after) r
                    WHERE s.relid = %s AND s.op = 'U' AND {join}""", (t,))
                applied += cur.rowcount

            for t in reversed(order):                             # child-first
                if not any(d["relid"] == t and d["op"] == "D" for d in delta):
                    continue
                pks, types = pk_cols(conn, t), col_types(conn, t)
                join = " AND ".join(f"(s.pk->>{sq(c)})::{types[c]} = _wfc.{q(c)}"
                                    for c in pks)
                cur.execute(f"""DELETE FROM {qt(t)} _wfc USING stg s
                    WHERE s.relid = %s AND s.op = 'D' AND {join}""", (t,))
                applied += cur.rowcount

            for stmt in restrict:                                # phase D
                cur.execute(stmt)

            # Merged rows carry the SANDBOX's generated keys, but the primary's
            # sequence never issued them: leave it behind and the next ordinary
            # INSERT on the primary fails with a duplicate key. Advance only.
            for t in sorted(set(tables) | new_tables):
                if t in dropped:
                    continue
                for r in rows(conn, """
                        SELECT a.attname AS c,
                               pg_get_serial_sequence(%s, a.attname) AS seq
                        FROM pg_attribute a
                        WHERE a.attrelid = %s::regclass AND a.attnum > 0
                          AND NOT a.attisdropped
                          AND pg_get_serial_sequence(%s, a.attname) IS NOT NULL""",
                        (qt(t), qt(t), qt(t))):
                    cur.execute(f"""SELECT setval(%s::regclass, x.m, true)
                        FROM (SELECT max({q(r['c'])}) m FROM {qt(t)}) x
                        WHERE x.m IS NOT NULL AND x.m >
                              coalesce(pg_sequence_last_value(%s::regclass), 0)""",
                        (r["seq"], r["seq"]))

            # execute-verify lite: final structure must equal the sandbox's
            for t in sorted(set(changeset["final_fp"]) - dropped):
                if table_fingerprint(conn, t) != changeset["final_fp"][t]:
                    raise SchemaDrift(f"post-apply structure of '{t}' does not "
                                      f"match the sandbox's final state")

        conn.commit()
        return ApplyResult("applied", applied, (time.monotonic() - t0) * 1000)
    except SchemaDrift as e:
        conn.rollback()
        return ApplyResult("schema_drift", 0, (time.monotonic() - t0) * 1000,
                           str(e))
    except DataConflict as e:
        conn.rollback()
        return ApplyResult("data_conflict", 0, (time.monotonic() - t0) * 1000,
                           f"{e.args[0]} drifted row(s) in {e.args[1]}")
    except psycopg2.errors.InsufficientPrivilege as e:
        conn.rollback()
        return ApplyResult("permission_denied", 0, (time.monotonic() - t0) * 1000,
                           str(e).strip()[:160])
    except psycopg2.Error as e:
        conn.rollback()
        return ApplyResult("error", 0, (time.monotonic() - t0) * 1000,
                           str(e).strip()[:200])
    except Exception as e:
        # anything else is a bug in this module, but it must still come back as
        # a RESULT: an exception escaping here leaves the transaction open and
        # the sandbox frozen with no way forward but a restart
        conn.rollback()
        return ApplyResult("error", 0, (time.monotonic() - t0) * 1000,
                           f"internal: {type(e).__name__}: "
                           f"{str(e).strip()[:160]}")


# ---------------------------------------------------------------- verification
def verify_conformance(conn, changeset):
    """The invariant that IS guaranteed under drift tolerance: every delta
    row landed exactly — I rows equal their after-image, U rows' changed
    columns equal the after values, D rows are gone. Returns violations.

    Comparison is on canonical text, not Python ==: Decimal is value-equal
    across scales, so == would call 100.000 and 100.0 identical."""
    bad = []
    with conn.cursor() as cur:
        pin_gucs(cur, local=False)
    for d in changeset["data_delta"]:
        t = d["relid"]
        types = col_types(conn, t)
        conds, args = [], []
        for k, v in d["pk"].items():
            conds.append(f"{q(k)} = %s::{types[k]}")
            args.append(v)
        r = rows(conn, f"SELECT to_jsonb(_wfc.*) j FROM {qt(t)} _wfc "
                       f"WHERE {' AND '.join(conds)}", args)
        row = r[0]["j"] if r else None
        if d["op"] == "D":
            ok = row is None
        elif d["op"] == "I":
            ok = json.same(row, d["after"])
        else:
            ok = row is not None and all(
                json.same(row.get(c), d["after"].get(c)) for c in d["changed"])
        if not ok:
            bad.append((t, d["op"], d["pk"], row))
    return bad


def residual_diff(pri, sbx, tables):
    """Full-table symmetric diff primary vs sandbox. After a merge this must
    equal EXACTLY the concurrent drift the merge tolerated — nothing else."""
    out = []
    for c in (pri, sbx):
        with c.cursor() as cur:
            pin_gucs(cur, local=False)
    for t in tables:
        # Counter, not set: a table without a unique constraint can hold the
        # same row twice, and set() would call 3 copies here and 1 there equal
        p = collections.Counter(
            json.jkey(r["j"])
            for r in rows(pri, f"SELECT to_jsonb(_wfc.*) j FROM {qt(t)} _wfc"))
        s = collections.Counter(
            json.jkey(r["j"])
            for r in rows(sbx, f"SELECT to_jsonb(_wfc.*) j FROM {qt(t)} _wfc"))
        out += [(t, "primary-only", x) for x in sorted((p - s).elements())]
        out += [(t, "sandbox-only", x) for x in sorted((s - p).elements())]
    return out
