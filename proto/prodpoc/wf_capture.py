"""Prod-PoC capture side, v3: M0 DML + M1/M2 DDL via catalog diff.

No SQL is ever captured. The artifact is {basis_ref, schema_delta,
data_delta}, both deltas derived from STATE:
- data_delta: first-touch journal (statement triggers, transition tables) +
  keyed join at capture. New tables (no triggers) capture as all-rows-INSERT;
  dropped tables discard their journal (the DDL covers them).
- schema_delta: catalog snapshot at detach vs catalog at capture, diffed into
  a WHITELISTED op list (create/drop table, add/drop column, not-null,
  defaults). Anything outside the whitelist (type changes, PK changes,
  constraint/index edits on existing tables) refuses capture — the agent is
  told to resync and redo within supported operations.
- Tables named scratch* are the sandbox scratch namespace: invisible to both
  deltas (prod: a policy-defined namespace).

A net state diff alone is NOT a sufficient gate: DDL fires no row triggers, so
any DDL sequence that returns the catalog to a whitelisted-looking shape while
rewriting or discarding rows would capture as an empty changeset. Six
history-aware gates close that:
  1. an event trigger snapshots the catalog after EVERY DDL, and the whitelist
     is enforced on each STEP (basis→s1→s2→…→final), not just on the net;
  2. the command TAG of every step must be one whose effect some snapshot can
     see (DDL_TAGS) — a view, function or COMMENT diffs to nothing at all;
  3. GUARD_SQL covers what no column diff can: triggers and their enabled
     state, rules, policies, RLS, reloptions, UNLOGGED;
  4. relfilenode is compared basis→final: a table rewritten by DDL (e.g.
     ALTER COLUMN TYPE … USING, which can leave the catalog identical) refuses;
  5. the rename hazard is judged over the union of step ops, so a
     drop+re-add or a three-way RENAME cycle cannot net itself invisible; a
     basis column dropped and re-added under the SAME name refuses outright,
     since the net catalog is unchanged and nothing would tell the primary to
     discard its own values;
  6. a table missing from the final catalog is only a DROP if its oid is gone
     — RENAME and SET SCHEMA merely move it out of the merge window, and
     treating that as a drop would destroy the primary's copy.

The row model (to_jsonb / jsonb_populate_record) is both the transport AND
what both verifiers compare, so a value it flattens is lost INVISIBLY: the
post-merge diff reads the same flattened shape on both sides and reports
success. value_gate() refuses the shapes it cannot represent — jsonb scalar
null, arrays with a non-default lower bound, float negative zero, type json —
on the basis at install AND on the final state at capture. For the same
reason every connection that renders or compares values pins the GUCs that
decide text output (STABLE_GUCS); extra_float_digits alone can cost five
digits of a float8.

Column attributes the server acts on — identity, GENERATED ... STORED and an
explicit collation — are part of the catalog model and the fingerprint. They
are reproduced on new tables and refused on existing ones; a generated column
is never carried as a merged value, since the primary recomputes it.

The journal's triggers are ENABLE ALWAYS: session_replication_role='replica'
is settable by any session and would otherwise silence them, capturing a
session's whole DML as an empty delta.
"""
import hashlib
import re

import psycopg2
import psycopg2.extras

import wf_json

wf_json.register()

json = wf_json           # every dump in this module must be Decimal-exact


def q(ident):
    """Quote an SQL identifier. Table/column names are attacker-chosen text:
    unquoted interpolation down-cases 'ShipLog', breaks on 'order', and would
    create a differently-named table on the primary."""
    return '"' + str(ident).replace('"', '""') + '"'


def qt(table):
    """Schema-qualified reference to a scoped relation (and to its rowtype).

    pg_catalog is implicitly FIRST in the search path, so a bare identifier
    loses to anything it shadows: NULL::"line" is the built-in geometric TYPE,
    not the table's rowtype ('line', 'point', 'money', 'path', 'date' and
    'interval' are all ordinary table names PostgreSQL accepts), and
    FROM "pg_class" is the catalog, not public.pg_class. Every relation and
    rowtype reference goes through this; q() is for columns."""
    return "public." + q(table)


def sq(text):
    """Quote a string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def rows(conn, sql, args=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def run(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


def pk_cols(conn, table):
    return [r["attname"] for r in rows(conn, """
        SELECT a.attname
        FROM pg_index i
        JOIN unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY k.ord""", (qt(table),))]


def col_types(conn, table):
    """Exact types (incl. precision) for casts and DDL generation."""
    return {r["col"]: r["typ"] for r in rows(conn, """
        SELECT a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ
        FROM pg_attribute a
        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped""",
        (qt(table),))}


def table_fingerprint(conn, table):
    """Structure hash. Uses format_type (information_schema's data_type
    collapses numeric(12,4) and numeric(12,1) to 'numeric') and the LIVE
    column ordinal, so a column that moved position is a drift, not a match."""
    return rows(conn, """
        SELECT md5(string_agg(x, '|' ORDER BY x)) AS fp FROM (
          SELECT row_number() OVER (ORDER BY a.attnum)||':'||a.attname||':'
                 ||format_type(a.atttypid, a.atttypmod)||':'||a.attnotnull
                 ||':'||coalesce(pg_get_expr(d.adbin, d.adrelid), '')
                 ||':'||a.attidentity::text||':'||a.attgenerated::text
                 ||':'||coalesce((SELECT co.collname FROM pg_collation co
                                  WHERE co.oid = a.attcollation
                                    AND a.attcollation <> ty.typcollation), '') AS x
          FROM pg_attribute a
          JOIN pg_type ty ON ty.oid = a.atttypid
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
          WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
          UNION ALL
          SELECT 'idx:'||indexdef FROM pg_indexes
          WHERE schemaname='public' AND tablename=%s
          UNION ALL
          SELECT 'con:'||conname||':'||pg_get_constraintdef(oid)
          FROM pg_constraint
          WHERE conrelid = %s::regclass
        ) t""", (qt(table), table, qt(table)))[0]["fp"]


# Single source of truth for "what the schema looks like": run from Python for
# the basis/final snapshots AND embedded in the DDL event trigger, so a
# step-wise diff can never disagree with the net diff over formatting.
CATALOG_SQL = """
SELECT coalesce(jsonb_object_agg(c.relname, jsonb_build_object(
  'order', (SELECT coalesce(jsonb_agg(a.attname ORDER BY a.attnum), '[]'::jsonb)
            FROM pg_attribute a
            WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
  'columns', (SELECT coalesce(jsonb_object_agg(a.attname, jsonb_build_object(
                       'type', format_type(a.atttypid, a.atttypmod),
                       'notnull', a.attnotnull,
                       -- for a GENERATED ... STORED column pg_attrdef holds the
                       -- GENERATION expression, not a default; 'generated' says
                       -- which of the two this is
                       'default', pg_get_expr(d.adbin, d.adrelid),
                       'identity', a.attidentity,
                       'generated', a.attgenerated,
                       'collation', (SELECT co.collname FROM pg_collation co
                                     WHERE co.oid = a.attcollation
                                       AND a.attcollation <> ty.typcollation))),
                     '{}'::jsonb)
              FROM pg_attribute a
              JOIN pg_type ty ON ty.oid = a.atttypid
              LEFT JOIN pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
              WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
  'pk', (SELECT coalesce(jsonb_agg(a.attname ORDER BY k.ord), '[]'::jsonb)
         FROM pg_index i
         JOIN unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true
         JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
         WHERE i.indrelid = c.oid AND i.indisprimary),
  'constraints', (SELECT coalesce(jsonb_agg(x ORDER BY x), '[]'::jsonb) FROM
         (SELECT pg_get_constraintdef(oid) x FROM pg_constraint
          WHERE conrelid = c.oid AND contype <> 'p') s),
  'indexes', (SELECT coalesce(jsonb_agg(x ORDER BY x), '[]'::jsonb) FROM
         (SELECT i.indexdef x FROM pg_indexes i
          WHERE i.schemaname = 'public' AND i.tablename = c.relname
            AND NOT EXISTS (SELECT 1 FROM pg_constraint pc
                            WHERE pc.conname = i.indexname
                              AND pc.conrelid = c.oid)) s)
)), '{}'::jsonb) AS cat
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND c.relname NOT LIKE 'wf%' AND c.relname NOT LIKE 'scratch%'
"""


# Table-level attributes that no column diff can see. Kept OUT of the catalog
# (which is also compared across databases) and out of the fingerprint: this is
# a sandbox-basis-vs-sandbox-final guard only. It is what makes CREATE TRIGGER,
# CREATE RULE, RLS, reloptions, UNLOGGED and DISABLE TRIGGER visible.
GUARD_SQL = """
SELECT coalesce(jsonb_object_agg(c.relname, jsonb_build_object(
  'persistence', c.relpersistence,
  'rls', c.relrowsecurity OR c.relforcerowsecurity,
  'reloptions', coalesce(to_jsonb(c.reloptions), '[]'::jsonb),
  'triggers', (SELECT coalesce(jsonb_agg(tg.tgname || ':' || tg.tgenabled::text
                                         ORDER BY tg.tgname), '[]'::jsonb)
               FROM pg_trigger tg
               WHERE tg.tgrelid = c.oid AND NOT tg.tgisinternal),
  'rules', (SELECT coalesce(jsonb_agg(r.rulename ORDER BY r.rulename),
                            '[]'::jsonb)
            FROM pg_rewrite r
            WHERE r.ev_class = c.oid AND r.rulename <> '_RETURN'),
  'policies', (SELECT coalesce(jsonb_agg(p.polname ORDER BY p.polname),
                               '[]'::jsonb)
               FROM pg_policy p WHERE p.polrelid = c.oid)
)), '{}'::jsonb) AS g
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  AND c.relname NOT LIKE 'wf%' AND c.relname NOT LIKE 'scratch%'
"""

CLEAN_GUARD = {"persistence": "p", "rls": False, "reloptions": [],
               "triggers": [], "rules": [], "policies": []}

# DDL whose effect ANOTHER gate can see. Tables and columns land in the
# catalog diff; indexes land in its 'indexes'/'constraints' lists; triggers,
# rules and policies land in the guard snapshot. Everything outside this set —
# views, matviews, functions, standalone sequences, types, domains,
# extensions, schemas, COMMENT, GRANT — changes nothing any snapshot compares,
# so the command tag is the ONLY evidence it happened, and silently dropping
# it on the floor is the failure mode. (Scoped bigserial and
# GENERATED ... AS IDENTITY do not emit a separate CREATE SEQUENCE.)
DDL_TAGS = {"CREATE TABLE", "CREATE TABLE AS", "SELECT INTO", "DROP TABLE",
            "ALTER TABLE",
            "CREATE INDEX", "ALTER INDEX", "DROP INDEX",
            "CREATE TRIGGER", "ALTER TRIGGER", "DROP TRIGGER",
            "CREATE RULE", "DROP RULE",
            "CREATE POLICY", "ALTER POLICY", "DROP POLICY"}

# GUCs that change how a value RENDERS. to_jsonb goes through each type's text
# output, so an unpinned session can silently truncate float8
# (extra_float_digits = -3 costs five digits) or reformat dates and intervals.
STABLE_GUCS = [("extra_float_digits", "3"), ("DateStyle", "ISO, MDY"),
               ("IntervalStyle", "postgres"), ("TimeZone", "UTC"),
               ("bytea_output", "hex"), ("lc_monetary", "C")]


def pin_gucs(cur, local=True):
    """Pin them on every connection that RENDERS or COMPARES values — the
    capture txn, the apply txn, and both verifiers. A verifier reading with
    different settings would compare two different renderings of the same
    row and call a correct merge broken (or the reverse)."""
    for k, v in STABLE_GUCS:
        cur.execute(f"SET {'LOCAL ' if local else ''}{k} = {sq(v)}")


def user_tables(conn):
    return sorted(catalog_snapshot(conn))


def guard_snapshot(conn):
    return rows(conn, GUARD_SQL)[0]["g"]


def table_oids(conn):
    """Identity that survives RENAME and SET SCHEMA — the only way to tell a
    table that LEFT the merge window from one that was dropped."""
    return {r["relname"]: r["oid"] for r in rows(conn, """
        SELECT c.relname, c.oid::bigint AS oid
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relname NOT LIKE 'wf%' AND c.relname NOT LIKE 'scratch%'""")}


def partitioned(conn):
    """Partitioned parents (relkind 'p') and partitions. The catalog model
    covers plain tables only: a parent is invisible to it and a partition looks
    like an ordinary new table, so the primary would get the partitions as
    unrelated tables with the routing and bounds silently gone."""
    return [r["relname"] for r in rows(conn, """
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND (c.relkind = 'p' OR c.relispartition)
          AND c.relname NOT LIKE 'wf%' AND c.relname NOT LIKE 'scratch%'""")]


def catalog_snapshot(conn):
    """Structured per-table catalog state (the schema-side 'before image')."""
    return rows(conn, CATALOG_SQL)[0]["cat"]


def relfilenodes(conn):
    """Physical file identity per table. A DDL that REWRITES a table changes
    it — the only signal for transformations the catalog cannot show."""
    return {r["relname"]: r["rfn"] for r in rows(conn, """
        SELECT c.relname, c.relfilenode::text AS rfn
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relname NOT LIKE 'wf%' AND c.relname NOT LIKE 'scratch%'""")}


def server_written(conn, table):
    """(generated-stored, identity) column names for a LIVE table.

    Both are written by the server, not by us: a STORED generated column
    rejects any explicit value, and a GENERATED ALWAYS AS IDENTITY column
    rejects one unless the INSERT says OVERRIDING SYSTEM VALUE."""
    r = rows(conn, """
        SELECT a.attname AS c, a.attgenerated AS g, a.attidentity AS i
        FROM pg_attribute a
        WHERE a.attrelid = %s::regclass AND a.attnum > 0
          AND NOT a.attisdropped""", (qt(table),))
    return ({x["c"] for x in r if x["g"]}, {x["c"] for x in r if x["i"]})


class SchemaChanged(Exception):
    """Sandbox schema moved outside the supported whitelist."""


def value_gate(conn, table, refuse):
    """Values the row model (to_jsonb / jsonb_populate_record) cannot carry.

    The row model is the whole protocol: it is the transport AND what both
    verifiers compare. A value it flattens is therefore not just lost, it is
    lost INVISIBLY — the post-merge diff reads the same flattened shape on
    both sides and reports success. Each of these is cheap to detect and
    impossible to represent, so capture refuses instead."""
    checks = []
    for c, ty in sorted(col_types(conn, table).items()):
        if ty == "json":
            # jsonb is a parsed value, json is stored text: round-tripping
            # rewrites key order and number literals and DROPS duplicate keys
            refuse.append(f"'{table}.{c}': type json cannot be carried "
                          f"losslessly (the row model normalizes it through "
                          f"jsonb: duplicate keys dropped, key order and "
                          f"number formatting rewritten) — use jsonb")
            continue
        if ty == "jsonb":
            # to_jsonb(row) puts a JSON null and an SQL NULL in the same slot,
            # and jsonb_populate_record turns both back into SQL NULL
            checks.append((c, f"{q(c)} = 'null'::jsonb",
                           "holds a JSON null scalar, which the row model "
                           "cannot tell from SQL NULL"))
        if ty.endswith("[]"):
            # to_jsonb emits the elements only; '[2:3]={x,y}' comes back as
            # '{x,y}' and every subscript in the primary's data shifts
            checks.append((c, f"{q(c)}::text LIKE '[%'",
                           "holds an array with a non-default lower bound, "
                           "which the row model does not carry"))
        if ty in ("double precision", "real"):
            # to_jsonb routes floats through numeric, which has no signed zero
            send = "float8send" if ty == "double precision" else "float4send"
            zero = "8000000000000000" if ty == "double precision" else "80000000"
            checks.append((c, f"{send}({q(c)}) = '\\x{zero}'::bytea",
                           "holds negative zero, which the row model "
                           "(numeric) cannot represent"))
    if not checks:
        return
    hit = rows(conn, "SELECT " + ", ".join(
        f"bool_or({e}) AS c{i}" for i, (_, e, _) in enumerate(checks))
        + f" FROM {qt(table)}")[0]
    for i, (c, _, why) in enumerate(checks):
        if hit[f"c{i}"]:
            refuse.append(f"'{table}.{c}': {why} — change the value or the "
                          f"column type")


def diff_catalog(basis, final):
    """basis→final catalog delta as whitelisted ops; anything else refuses.

    Refusals are (table, message) pairs: the step-wise history gate replays
    this over intermediate catalog states, where a table that did not exist at
    the basis is still under construction (CREATE TABLE then ADD PRIMARY KEY
    is two states) and only its NET shape is worth judging."""
    ops, refuse = [], []
    for t in sorted(set(final) - set(basis)):                    # new tables
        f = final[t]
        if f["constraints"] or f["indexes"]:
            refuse.append((t, f"new table '{t}' carries non-PK "
                              f"constraints/indexes — out of PoC whitelist"))
        ops.append({"op": "create_table", "table": t, "order": f["order"],
                    "columns": f["columns"], "pk": f["pk"]})
    for t in sorted(set(basis) - set(final)):                    # dropped
        ops.append({"op": "drop_table", "table": t})
    for t in sorted(set(basis) & set(final)):
        b, f = basis[t], final[t]
        if b["pk"] != f["pk"]:
            refuse.append((t, f"'{t}': primary key changed — not supported"))
        if b["constraints"] != f["constraints"] or b["indexes"] != f["indexes"]:
            refuse.append((t, f"'{t}': constraint/index changes on an existing "
                              f"table — M3, not in whitelist"))
        bc, fc = b["columns"], f["columns"]
        # surviving basis columns must keep their relative order: the primary
        # applies ADD COLUMN, which can only append, so a reordering is
        # unreproducible. Caught here (actionable) instead of as an opaque
        # post-apply fingerprint drift.
        keep = [c for c in b["order"] if c in fc]
        if keep != [c for c in f["order"] if c in bc]:
            refuse.append((t, f"'{t}': existing columns were reordered — the "
                              f"primary can only append columns; recreate the "
                              f"order or resync"))
        # added columns emitted in the sandbox's own column order, so the
        # primary ends up with the same live ordinals (see table_fingerprint)
        for c in [x for x in f["order"] if x not in bc]:
            d = fc[c]["default"]
            if fc[c].get("generated") or fc[c].get("identity"):
                # both backfill every existing row server-side with no row
                # trigger firing (and GENERATED ... STORED rewrites the table)
                refuse.append((t, f"'{t}.{c}': ADD COLUMN GENERATED/IDENTITY "
                                  f"on an existing table backfills every row "
                                  f"implicitly — not in whitelist; add a plain "
                                  f"column and backfill explicitly"))
                continue
            if d and "(" in d:
                # non-constant default on ADD COLUMN backfills every existing
                # row IMPLICITLY (no row triggers fire): sandbox and primary
                # would evaluate it independently and diverge
                refuse.append((t, f"'{t}.{c}': ADD COLUMN with non-constant "
                                  f"default {d!r} — implicit backfill "
                                  f"diverges; add nullable + backfill "
                                  f"explicitly"))
            ops.append({"op": "add_column", "table": t, "col": c,
                        "type": fc[c]["type"], "default": d})
            if fc[c]["notnull"]:
                ops.append({"op": "set_not_null", "table": t, "col": c})
        for c in sorted(set(bc) - set(fc)):
            ops.append({"op": "drop_column", "table": t, "col": c})
        for c in sorted(set(bc) & set(fc)):
            if bc[c]["type"] != fc[c]["type"]:
                refuse.append((t, f"'{t}.{c}': type change {bc[c]['type']} "
                                  f"-> {fc[c]['type']} — M1 whitelist excludes "
                                  f"type changes (table rewrite / long lock)"))
            # identity / generated / collation are silent in every other signal
            # we have: dropping one loses server-side value generation, adding
            # one rewrites rows. Any move refuses. ('' vs missing: a catalog
            # snapshot taken before these keys existed reads as None.)
            for attr in ("identity", "generated", "collation"):
                if (bc[c].get(attr) or None) != (fc[c].get(attr) or None):
                    refuse.append(
                        (t, f"'{t}.{c}': {attr} changed "
                            f"({bc[c].get(attr) or 'none'} -> "
                            f"{fc[c].get(attr) or 'none'}) — not in whitelist"))
            if not bc[c]["notnull"] and fc[c]["notnull"]:
                ops.append({"op": "set_not_null", "table": t, "col": c})
            if bc[c]["notnull"] and not fc[c]["notnull"]:
                ops.append({"op": "drop_not_null", "table": t, "col": c})
            if bc[c]["default"] != fc[c]["default"]:
                if fc[c].get("generated"):
                    # not a default: PG has no ALTER for a generation
                    # expression, it is a drop-and-add (i.e. a rewrite)
                    refuse.append((t, f"'{t}.{c}': generation expression "
                                      f"changed — not in whitelist"))
                else:
                    ops.append({"op": "set_default", "table": t, "col": c,
                                "default": fc[c]["default"]})
    return ops, refuse


WF_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS wf;
CREATE TABLE IF NOT EXISTS wf.journal(
  relid  text  NOT NULL,
  pk     jsonb NOT NULL,
  before jsonb,
  PRIMARY KEY (relid, pk));
CREATE TABLE IF NOT EXISTS wf.meta(k text PRIMARY KEY, v jsonb);
CREATE TABLE IF NOT EXISTS wf.ddl_log(
  seq bigserial PRIMARY KEY, tag text, cat jsonb);
"""


def _fname(kind, table):
    """Trigger function name: safe and collision-free for any table name
    (identifiers truncate at 63 bytes, so long names would alias)."""
    slug = re.sub(r"[^A-Za-z0-9_]", "_", table)[:24]
    return f"j_{kind}_{slug}_{hashlib.md5(table.encode()).hexdigest()[:8]}"


def install(conn, tables):
    """Instrument scoped tables + snapshot the full catalog (the basis).
    Once per clone: re-installing would re-baseline the catalog while the
    journal still holds before-images against the OLD basis."""
    run(conn, WF_SCHEMA)
    if rows(conn, "SELECT 1 FROM wf.meta WHERE k='scope'"):
        conn.rollback()
        raise RuntimeError("already installed on this clone — one basis per "
                           "clone; resync to start a new session")
    part = partitioned(conn)
    if part:
        conn.rollback()
        raise RuntimeError(f"partitioned tables are out of PoC scope "
                           f"({', '.join(sorted(part))}): the catalog model "
                           f"has no partition bounds, so they would land on "
                           f"the primary as unrelated plain tables")
    run(conn, """CREATE OR REPLACE FUNCTION wf.no_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'TRUNCATE is not capturable (fires no row triggers) '
                          '— use DELETE';
        END $$""")
    # DDL history: the catalog after every DDL statement. Guarded on 'scope'
    # so install's own DDL is not logged; capture enforces the whitelist on
    # each step, which is what makes net-zero DDL sequences visible.
    run(conn, f"""CREATE OR REPLACE FUNCTION wf.on_ddl() RETURNS event_trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM wf.meta WHERE k = 'scope') THEN
            INSERT INTO wf.ddl_log(tag, cat)
            VALUES (tg_tag, ({CATALOG_SQL}));
          END IF;
        END $$""")
    run(conn, "DROP EVENT TRIGGER IF EXISTS wf_ddl_snap")
    run(conn, "CREATE EVENT TRIGGER wf_ddl_snap ON ddl_command_end "
              "EXECUTE FUNCTION wf.on_ddl()")
    for t in tables:
        pks = pk_cols(conn, t)
        if not pks:
            raise RuntimeError(f"{t}: no primary key — out of scope")
        pk_o = ", ".join(f"{sq(c)}, o.{q(c)}" for c in pks)
        pk_n = ", ".join(f"{sq(c)}, n.{q(c)}" for c in pks)
        pk_eq = " AND ".join(f"o.{q(c)} = n.{q(c)}" for c in pks)
        f_ins, f_upd, f_del = (_fname(k, t) for k in ("ins", "upd", "del"))
        # to_jsonb(o.*) not to_jsonb(o): a column literally named 'o' would
        # shadow the whole-row alias and journal a scalar instead of the row
        run(conn, f"""
CREATE OR REPLACE FUNCTION wf.{q(f_ins)}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO wf.journal
  SELECT {sq(t)}, jsonb_build_object({pk_n}), NULL FROM new_rows n
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION wf.{q(f_upd)}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO wf.journal
  SELECT {sq(t)}, jsonb_build_object({pk_o}), to_jsonb(o.*) FROM old_rows o
  ON CONFLICT DO NOTHING;
  INSERT INTO wf.journal
  SELECT {sq(t)}, jsonb_build_object({pk_n}), NULL FROM new_rows n
  WHERE NOT EXISTS (SELECT 1 FROM old_rows o WHERE {pk_eq})
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION wf.{q(f_del)}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO wf.journal
  SELECT {sq(t)}, jsonb_build_object({pk_o}), to_jsonb(o.*) FROM old_rows o
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS wf_j_ins ON {qt(t)};
DROP TRIGGER IF EXISTS wf_j_upd ON {qt(t)};
DROP TRIGGER IF EXISTS wf_j_del ON {qt(t)};
CREATE TRIGGER wf_j_ins AFTER INSERT ON {qt(t)}
  REFERENCING NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION wf.{q(f_ins)}();
CREATE TRIGGER wf_j_upd AFTER UPDATE ON {qt(t)}
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION wf.{q(f_upd)}();
CREATE TRIGGER wf_j_del AFTER DELETE ON {qt(t)}
  REFERENCING OLD TABLE AS old_rows
  FOR EACH STATEMENT EXECUTE FUNCTION wf.{q(f_del)}();
DROP TRIGGER IF EXISTS wf_no_truncate ON {qt(t)};
CREATE TRIGGER wf_no_truncate BEFORE TRUNCATE ON {qt(t)}
  FOR EACH STATEMENT EXECUTE FUNCTION wf.no_truncate();
-- ALWAYS, not the ORIGIN default: session_replication_role = 'replica' is
-- settable by any session and silences ORIGIN triggers, which would make
-- every row change invisible to the journal and capture as an empty delta
ALTER TABLE {qt(t)} ENABLE ALWAYS TRIGGER wf_j_ins;
ALTER TABLE {qt(t)} ENABLE ALWAYS TRIGGER wf_j_upd;
ALTER TABLE {qt(t)} ENABLE ALWAYS TRIGGER wf_j_del;
ALTER TABLE {qt(t)} ENABLE ALWAYS TRIGGER wf_no_truncate;""")
    # Also gate the BASIS values, not just the final ones. A value the row
    # model cannot represent is a hazard in BOTH directions: sandbox
    # 'null'::jsonb -> SQL NULL renders identically on both sides of capture's
    # own before/after text compare, so it produces no delta row at all and
    # the primary silently keeps the old value. Refusing at install also means
    # the agent hears about it before doing an hour of work.
    basis_bad = []
    for t in tables:
        value_gate(conn, t, basis_bad)
    if basis_bad:
        conn.rollback()
        raise RuntimeError("cannot instrument this clone: "
                           + "; ".join(basis_bad))
    catalog = catalog_snapshot(conn)
    basis_fp = {t: table_fingerprint(conn, t) for t in tables}
    run(conn, "INSERT INTO wf.meta VALUES ('scope', %s), ('basis_fp', %s), "
              "('catalog', %s), ('basis_rfn', %s), ('basis_oid', %s), "
              "('basis_guards', %s) "
              "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
        (json.dumps(tables), json.dumps(basis_fp), json.dumps(catalog),
         json.dumps(relfilenodes(conn)), json.dumps(table_oids(conn)),
         json.dumps(guard_snapshot(conn))))
    conn.commit()
    return basis_fp


def history_gate(conn, meta, basis_cat, final_cat, refuse):
    """Whitelist the DDL HISTORY, not just its net effect. Returns the union
    of per-step ops (used by the rename-hazard guard)."""
    log = rows(conn, "SELECT tag, cat FROM wf.ddl_log ORDER BY seq")
    # the catalog diff only sees TABLES and COLUMNS. A DDL whose whole effect
    # lives elsewhere (CREATE VIEW / FUNCTION / TRIGGER / RULE / POLICY /
    # SEQUENCE / SCHEMA, COMMENT) diffs to nothing, so the tag itself is the
    # only evidence. Every operation this PoC supports — including bigserial
    # and GENERATED ... AS IDENTITY, which do NOT emit a separate
    # CREATE SEQUENCE — reports one of DDL_TAGS.
    for tag in sorted({r["tag"] for r in log} - DDL_TAGS):
        refuse.append(f"unsupported DDL in this session: {tag} — only table "
                      f"and column changes can be merged (schema-level and "
                      f"non-table objects are never carried)")
    states = [basis_cat] + [r["cat"] for r in log]
    step_ops, seen = [], set()
    for a, b in zip(states, states[1:]):
        ops, bad = diff_catalog(a, b)
        step_ops += ops
        for t, m in bad:
            # a table absent from the basis is still under construction at an
            # intermediate state (CREATE TABLE, then ADD PRIMARY KEY); only
            # its final shape is judged, by the net diff
            if t in basis_cat and m not in seen:
                seen.add(m)
                refuse.append(f"intermediate DDL state: {m}")
    # a table dropped and recreated nets to zero in the catalog while every
    # row is gone; the net diff would emit no op at all
    for t in sorted({o["table"] for o in step_ops if o["op"] == "drop_table"}
                    & set(final_cat)):
        refuse.append(f"'{t}': table was dropped and recreated during the "
                      f"session — the row set is unrecoverable from a state "
                      f"diff; resync and redo")
    # …and the same trick one level down: DROP COLUMN x + ADD COLUMN x of the
    # same type nets to zero in the catalog, so no op is emitted, while the
    # sandbox's column now holds the ADD-time backfill for every row and the
    # primary still holds the original data. Rows whose backfill value happens
    # to survive to the final state produce no 'changed' entry either, so the
    # divergence is invisible to the delta AND to a post-apply state compare.
    dropped_cols, added_cols = {}, {}
    for o in step_ops:
        if o["op"] == "drop_column":
            dropped_cols.setdefault(o["table"], set()).add(o["col"])
        if o["op"] == "add_column":
            added_cols.setdefault(o["table"], set()).add(o["col"])
    for t in sorted(set(dropped_cols) & set(added_cols) & set(final_cat)):
        for c in sorted(dropped_cols[t] & added_cols[t]
                        & set(basis_cat.get(t, {}).get("columns", {}))
                        & set(final_cat[t]["columns"])):
            refuse.append(f"'{t}.{c}': column was dropped and re-added during "
                          f"the session — the net catalog looks unchanged, so "
                          f"nothing would tell the primary to discard its own "
                          f"values for it; resync, or use a new column name")
    # physical rewrite: ALTER COLUMN TYPE ... USING can transform every row
    # while leaving the catalog byte-identical
    basis_rfn = meta.get("basis_rfn", {})
    now_rfn = relfilenodes(conn)
    for t in sorted(set(basis_rfn) & set(now_rfn) & set(final_cat)):
        if basis_rfn[t] != now_rfn[t]:
            refuse.append(f"'{t}': table was REWRITTEN by DDL (relfilenode "
                          f"changed) — rows may have been transformed with no "
                          f"catalog or journal trace; resync and redo")
    # a table that LEFT the merge window (RENAME, or SET SCHEMA) is missing
    # from the final catalog and therefore indistinguishable from a DROP — and
    # a drop_table op would delete the primary's copy, data and all. Its oid
    # survives both, so ask the catalog whether it is really gone.
    for t in sorted(set(basis_cat) - set(final_cat)):
        oid = meta.get("basis_oid", {}).get(t)
        if oid is None:
            continue
        now = rows(conn, """SELECT n.nspname || '.' || c.relname AS nm
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = %s::oid""", (oid,))
        if now:
            refuse.append(f"'{t}': the table still exists as "
                          f"'{now[0]['nm']}' — it was renamed or moved out of "
                          f"the merge window, which a state diff cannot tell "
                          f"from a DROP; merging would destroy the primary's "
                          f"table. Move it back, or drop it for real.")
    # table-level attributes no column diff can see. Skipped outright on a
    # clone installed before basis_guards existed: with no basis to compare,
    # every scoped table would look like it had grown the journal's own
    # triggers. Such a clone must be resynced to get the gate.
    basis_g, final_g = meta.get("basis_guards"), guard_snapshot(conn)
    for t in sorted(set(final_g) if basis_g is not None else ()):
        want = basis_g.get(t, CLEAN_GUARD)
        for k in sorted(CLEAN_GUARD):
            if want.get(k, CLEAN_GUARD[k]) != final_g[t].get(k):
                refuse.append(
                    f"'{t}': table attribute '{k}' changed "
                    f"({want.get(k, CLEAN_GUARD[k])!r} -> "
                    f"{final_g[t].get(k)!r}) — triggers, rules, policies, RLS, "
                    f"storage options and UNLOGGED are not part of a data "
                    f"merge and would not reach the primary")
    return step_ops


def capture(conn):
    """REPEATABLE READ txn: catalog diff (whitelist gate) + keyed data joins.
    Returns {basis_fp, final_fp, schema_delta, data_delta, stats}."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        pin_gucs(cur)          # to_jsonb renders through each type's text out
    meta = {r["k"]: r["v"] for r in rows(conn, "SELECT k, v FROM wf.meta")}
    scope, basis_fp, basis_cat = meta["scope"], meta["basis_fp"], meta["catalog"]

    final_cat = catalog_snapshot(conn)
    schema_delta, bad = diff_catalog(basis_cat, final_cat)
    refuse = [m for _, m in bad]
    step_ops = history_gate(conn, meta, basis_cat, final_cat, refuse)
    for t in sorted(partitioned(conn)):
        refuse.append(f"'{t}': partitioned tables are out of PoC scope — the "
                      f"catalog model carries no partition bounds")
    for o in schema_delta:
        if o["op"] == "create_table" and not o["pk"]:
            refuse.append(f"new table '{o['table']}' has no primary key — "
                          f"rows cannot be keyed for a merge; add one")
    for t in sorted(set(scope) & set(final_cat)
                    | (set(final_cat) - set(basis_cat))):
        value_gate(conn, t, refuse)

    # rename hazard: a BASIS column dropped and a FINAL column added on the
    # same table looks like a rename; values only survive if the agent
    # explicitly rewrote EVERY row (journal covers the table). Judged over the
    # union of step ops and net ops, so a rename cycle or a drop+re-add that
    # nets to nothing still trips it. Add/drop of a column that only ever
    # existed mid-session is harmless and does not count.
    hazard = {}
    for o in step_ops + schema_delta:
        t = o["table"]
        if o["op"] == "drop_column" and o["col"] in basis_cat.get(
                t, {}).get("columns", {}):
            hazard.setdefault(t, set()).add("drop")
        if o["op"] == "add_column" and o["col"] in final_cat.get(
                t, {}).get("columns", {}):
            hazard.setdefault(t, set()).add("add")
    for t, kinds in sorted(hazard.items()):
        if kinds == {"add", "drop"} and t in basis_cat and t in final_cat:
            pks = pk_cols(conn, t)
            pk_c = ", ".join(f"{sq(c)}, _wfc.{q(c)}" for c in pks)
            n = rows(conn, f"""SELECT count(*) n FROM {qt(t)} _wfc
                WHERE NOT EXISTS (SELECT 1 FROM wf.journal j
                    WHERE j.relid = %s
                      AND j.pk = jsonb_build_object({pk_c}))""", (t,))[0]["n"]
            if n:
                refuse.append(f"'{t}': column dropped AND added (rename?) but "
                              f"{n} row(s) were never rewritten — their values "
                              f"in the new column would be lost; backfill every "
                              f"row or split the change")
    # ADD COLUMN implicitly backfills existing rows — with the default AT ADD
    # TIME, which the final catalog cannot tell us (the default may have been
    # changed afterwards). The DATA still knows: rows never journaled carry
    # exactly the ADD-time backfill value V. Reconstruct V from them, make the
    # generated ADD use V, and emit a set_default op if the final catalog
    # default differs. Journaled before-images that predate the ADD get V
    # patched in, so the OCC comparison matches the primary post-relax state.
    added_defaults = {}
    fixed_ops = []
    for o in schema_delta:
        fixed_ops.append(o)
        if o["op"] != "add_column" or o["table"] not in basis_cat:
            continue
        t, col, typ, final_expr = o["table"], o["col"], o["type"], o["default"]
        pks = pk_cols(conn, t)
        pk_j = ", ".join(f"{sq(c)}, _wfc.{q(c)}" for c in pks)
        # two independent evidence sources for the ADD-time backfill value:
        # rows never touched (still carry it), and journal before-images whose
        # first touch happened after the ADD (OLD row carried it). Either may
        # be empty — an adversary can exhaust one, not both, without leaving
        # the value recoverable-by-construction.
        u = rows(conn, f"""SELECT DISTINCT to_jsonb(_wfc.{q(col)}) AS v
            FROM {qt(t)} _wfc
            WHERE NOT EXISTS (SELECT 1 FROM wf.journal j
                WHERE j.relid = %s AND j.pk = jsonb_build_object({pk_j}))
            LIMIT 2""", (t,))
        ev = rows(conn, """SELECT DISTINCT j.before->%s AS v FROM wf.journal j
            WHERE j.relid = %s AND j.before IS NOT NULL AND j.before ? %s
            LIMIT 2""", (col, t, col))
        vals = {json.jkey(x["v"]) for x in list(u) + list(ev)}
        if len(vals) > 1:
            refuse.append(f"'{t}.{col}': conflicting evidence for the added "
                          f"column's implicit backfill value — refuse; "
                          f"backfill explicitly")
            continue
        if vals:
            V = json.loads(vals.pop())
        elif final_expr:
            V = rows(conn, f"SELECT to_jsonb(({final_expr})::{typ}) AS v")[0]["v"]
        else:
            V = None
        if V is None:
            o["default"] = None
        elif typ in ("json", "jsonb"):
            # V came out of to_jsonb already DECODED, so a JSON string arrives
            # as a Python str: binding it directly would make '"5"'::jsonb into
            # the literal '5'::jsonb (a number) and '"hi"'::jsonb an outright
            # parse error. Re-encode to JSON text first.
            with conn.cursor() as _cu:
                o["default"] = _cu.mogrify(f"%s::{typ}",
                                           (json.dumps(V),)).decode()
        elif isinstance(V, dict):
            refuse.append(f"'{t}.{col}': non-scalar implicit backfill value — "
                          f"out of PoC scope")
            continue
        else:
            # lists reach here only for array-typed columns, where psycopg2's
            # own list adaptation produces the right literal
            with conn.cursor() as _cu:
                o["default"] = _cu.mogrify(f"%s::{typ}", (V,)).decode()
        added_defaults.setdefault(t, {})[col] = V
        if (o["default"] or None) != (final_expr or None):
            fixed_ops.append({"op": "set_default", "table": t, "col": col,
                              "default": final_expr})
    schema_delta = fixed_ops

    if refuse:
        conn.rollback()
        raise SchemaChanged("capture refused — resync and redo within the "
                            "whitelist: " + "; ".join(refuse))

    def mergeable(t, types):
        """Columns the apply side may WRITE. A STORED generated column is
        recomputed by the primary from its own base columns; carrying it would
        only give us a value PostgreSQL refuses to accept."""
        gen = {c for c, d in final_cat[t]["columns"].items() if d.get("generated")}
        return sorted(set(types) - gen)

    late = []
    delta = []
    for t in scope:                                   # journaled, still present
        if t not in final_cat:
            continue                                  # dropped: DDL covers it
        patch = added_defaults.get(t, {})
        pks, types = pk_cols(conn, t), col_types(conn, t)
        join = " AND ".join(f"(j.pk->>{sq(c)})::{types[c]} = _wfc.{q(c)}"
                            for c in pks)
        cols = mergeable(t, types)
        ident_always = {c for c, d in final_cat[t]["columns"].items()
                        if d.get("identity") == "a"}
        for e in rows(conn, f"""
                SELECT j.pk, j.before, to_jsonb(_wfc.*) AS after
                FROM wf.journal j LEFT JOIN {qt(t)} _wfc ON {join}
                WHERE j.relid = %s
                  AND j.before::text IS DISTINCT FROM to_jsonb(_wfc.*)::text""",
                (t,)):
            before, after = e["before"], e["after"]
            if before is not None and patch:
                before = {**{c: v for c, v in patch.items()}, **before}
            if before is None:
                op, changed = "I", cols
            elif after is None:
                op, changed = "D", cols
            else:
                op = "U"
                # changed evaluated over FINAL columns only: values living in
                # dropped columns are the DDL's business, not the DML's.
                # jkey, not ==: Decimal('100.0') == Decimal('100.000')
                changed = [c for c in cols
                           if not json.same(before.get(c), after.get(c))]
                if not changed:
                    continue
                if set(changed) & ident_always:
                    # PostgreSQL rejects UPDATE of a GENERATED ALWAYS AS
                    # IDENTITY column outright; better to say so than to fail
                    # the whole merge at apply
                    late.append(f"'{t}': a GENERATED ALWAYS AS IDENTITY column "
                                f"({', '.join(sorted(set(changed) & ident_always))}) "
                                f"was updated — the primary cannot accept that "
                                f"value; redo without touching it")
            delta.append({"relid": t, "op": op, "pk": e["pk"],
                          "before": before, "after": after, "changed": changed})
    for t in sorted(set(final_cat) - set(basis_cat)):  # new tables: full scan
        pks = pk_cols(conn, t)
        cols = mergeable(t, col_types(conn, t))
        for r in rows(conn, f"SELECT to_jsonb(_wfc.*) AS j FROM {qt(t)} _wfc"):
            delta.append({"relid": t, "op": "I",
                          "pk": {c: r["j"][c] for c in pks},
                          "before": None, "after": r["j"], "changed": cols})

    if late:
        conn.rollback()
        raise SchemaChanged("capture refused: " + "; ".join(sorted(set(late))))

    final_fp = {t: table_fingerprint(conn, t) for t in final_cat}
    journal_n = rows(conn, "SELECT count(*) n FROM wf.journal")[0]["n"]
    conn.rollback()
    return {"basis_fp": basis_fp, "final_fp": final_fp,
            "schema_delta": schema_delta, "data_delta": delta,
            "stats": {"journal_rows": journal_n, "delta_rows": len(delta),
                      "ddl_ops": len(schema_delta)}}
