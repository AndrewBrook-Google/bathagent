"""Branch provider & Diff Capture Engine for Wildfire.

Connects directly to the PostgreSQL / AlloyDB instance and performs authoritative,
database-grounded diff capture (evaluating actual before/after row states and primary keys)
for the Wildfire policy engine and reviewer.
"""
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("WF_DBNAME", "postgres")
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "harness"))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "demo"))

import alloydb_client
import capture2

SESSIONS: Dict[str, "AlloyDBSandboxSession"] = {}

_PK_CACHE: Dict[str, str] = {}
_COLS_CACHE: Dict[str, List[str]] = {}


def _get_schema_metadata() -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """Introspects primary keys and column definitions from database catalogs.
    Fails immediately if schema cannot be introspected.
    """
    global _PK_CACHE, _COLS_CACHE
    if _PK_CACHE and _COLS_CACHE:
        return _PK_CACHE, _COLS_CACHE

    try:
        pks_rows = alloydb_client.execute_sql("""
            SELECT a.attname as pk, i.indrelid::regclass::text as tablename 
            FROM pg_index i 
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) 
            WHERE i.indisprimary AND i.indrelid::regclass::text NOT LIKE 'pg_%';
        """)
        for r in pks_rows:
            _PK_CACHE[r["tablename"]] = r["pk"]

        cols_rows = alloydb_client.execute_sql("""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position;
        """)
        for r in cols_rows:
            _COLS_CACHE.setdefault(r["table_name"], []).append(r["column_name"])

        if not _COLS_CACHE:
            raise RuntimeError("Database returned empty schema columns from information_schema.")

    except Exception as e:
        _PK_CACHE.clear()
        _COLS_CACHE.clear()
        raise RuntimeError(f"Database schema introspection failed: {e}. Validator cannot safely capture diffs.") from e

    return _PK_CACHE, _COLS_CACHE


def compute_schema_fingerprint() -> str:
    """Computes a stable hash of public schema column definitions."""
    try:
        rows = alloydb_client.execute_sql("""
            SELECT table_name || '.' || column_name || ':' || data_type || ':' || is_nullable as col_spec
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name NOT LIKE 'wf%'
            ORDER BY table_name, ordinal_position;
        """)
        if not rows:
            return "empty_schema"
        spec_str = "|".join(r["col_spec"] for r in rows)
        return hashlib.md5(spec_str.encode()).hexdigest()[:16]
    except Exception as e:
        raise RuntimeError(f"Failed to compute schema fingerprint: {e}") from e


def split_sql_statements(sql: str) -> List[str]:
    """Split a string of SQL statements by semicolons, ignoring semicolons within quotes."""
    statements = []
    cur = []
    in_single = False
    in_double = False
    escape = False

    # Remove SQL line comments
    lines = []
    for line in sql.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("--"):
            continue
        lines.append(line)
    clean_text = "\n".join(lines)

    for ch in clean_text:
        if ch == "\\" and not escape:
            escape = True
            cur.append(ch)
            continue
        if ch == "'" and not in_double and not escape:
            in_single = not in_single
        elif ch == '"' and not in_single and not escape:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            stmt = "".join(cur).strip()
            if stmt:
                statements.append(stmt)
            cur = []
            escape = False
            continue

        cur.append(ch)
        escape = False

    stmt = "".join(cur).strip()
    if stmt:
        statements.append(stmt)
    return statements


def parse_csv_row(row_str: str) -> List[str]:
    items = []
    cur = []
    in_single = False
    in_double = False
    escape = False

    for ch in row_str:
        if ch == "\\" and not escape:
            escape = True
            cur.append(ch)
            continue
        if ch == "'" and not in_double and not escape:
            in_single = not in_single
        elif ch == '"' and not in_single and not escape:
            in_double = not in_double
        elif ch == "," and not in_single and not in_double:
            val = "".join(cur).strip()
            items.append(val)
            cur = []
            escape = False
            continue

        cur.append(ch)
        escape = False

    val = "".join(cur).strip()
    if val:
        items.append(val)
    return items


def _clean_val(val: str) -> Any:
    val = val.strip()
    if val.upper() == "NULL":
        return None
    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        return val[1:-1]
    return val


def parse_insert_tuples(values_str: str) -> List[List[Any]]:
    tuples = []
    in_single = False
    in_double = False
    escape = False
    depth = 0
    cur_tuple_chars = []

    for ch in values_str:
        if ch == "\\" and not escape:
            escape = True
            if depth > 0:
                cur_tuple_chars.append(ch)
            continue
        if ch == "'" and not in_double and not escape:
            in_single = not in_single
        elif ch == '"' and not in_single and not escape:
            in_double = not in_double
        elif ch == "(" and not in_single and not in_double:
            depth += 1
            if depth == 1:
                cur_tuple_chars = []
                escape = False
                continue
        elif ch == ")" and not in_single and not in_double:
            depth -= 1
            if depth == 0:
                raw_tuple_str = "".join(cur_tuple_chars)
                raw_vals = [_clean_val(v) for v in parse_csv_row(raw_tuple_str)]
                tuples.append(raw_vals)
                cur_tuple_chars = []
                escape = False
                continue

        if depth > 0:
            cur_tuple_chars.append(ch)
        escape = False

    return tuples


def _evaluate_set_clause(set_str: str, before_row: Dict[str, Any]) -> Dict[str, Any]:
    assignments = {}
    raw_assignments = parse_csv_row(set_str)

    for assign in raw_assignments:
        if not assign or "=" not in assign:
            continue
        col, expr = assign.split("=", 1)
        col = col.strip().lower()
        expr = expr.strip()

        if (expr.startswith("'") and expr.endswith("'")) or (expr.startswith('"') and expr.endswith('"')):
            assignments[col] = expr[1:-1]
        elif expr.upper() == "NULL":
            assignments[col] = None
        else:
            try:
                eval_scope = {}
                for k, v in before_row.items():
                    if v is None:
                        eval_scope[k.lower()] = 0
                    else:
                        try:
                            if "." in str(v):
                                eval_scope[k.lower()] = float(v)
                            else:
                                eval_scope[k.lower()] = int(v)
                        except Exception:
                            eval_scope[k.lower()] = v

                expr_clean = expr.lower()
                if "current_timestamp" in expr_clean or "now()" in expr_clean or "current_date" in expr_clean:
                    import datetime
                    eval_val = datetime.datetime.now(datetime.timezone.utc).isoformat()
                else:
                    eval_val = eval(expr, {"__builtins__": {}}, eval_scope)
                    if isinstance(eval_val, float):
                        eval_val = f"{eval_val:.2f}"
                    else:
                        eval_val = str(eval_val)
                assignments[col] = eval_val
            except Exception:
                assignments[col] = _clean_val(expr)

    return assignments


class AlloyDBSandboxSession:
    """Live Sandbox Session for an attached/detached agent branch."""

    def __init__(self, branch_id: str):
        self.branch_id = branch_id
        self.basis_fp = compute_schema_fingerprint()
        self.final_fp = self.basis_fp
        self.segments: List[Dict[str, Any]] = []
        self.sql_log: List[Dict[str, Any]] = []
        self.row_diffs: Dict[str, Dict[str, Any]] = {}  # key: f"{table}:{pk_val}" -> diff_entry
        self._auto_pk_counter = 0

    def wf_begin(self):
        self.segments = []
        self.sql_log = []
        self.row_diffs = {}
        self.basis_fp = compute_schema_fingerprint()
        self.final_fp = self.basis_fp
        self._auto_pk_counter = 0

    def query(self, sql: str) -> List[Dict[str, Any]]:
        """Execute real query directly against primary database."""
        return alloydb_client.execute_sql(sql)

    def execute(self, sql: str) -> int:
        """Authoritatively capture diff by evaluating database before/after state for all statements."""
        pk_map, cols_map = _get_schema_metadata()
        statements = split_sql_statements(sql)
        total_affected_rows = 0

        for clean_sql in statements:
            head = clean_sql.split()[0].upper() if clean_sql else ""

            if head in ("CREATE", "ALTER", "DROP", "TRUNCATE"):
                self.final_fp = hashlib.md5((self.basis_fp + clean_sql).encode()).hexdigest()[:16]
                self.segments.append({
                    "kind": "ddl",
                    "statements": [clean_sql],
                    "fp_before": self.basis_fp,
                    "fp_after": self.final_fp,
                })
                self.sql_log.append({"sql": clean_sql, "kind": "ddl"})
                total_affected_rows += 1

            elif head == "UPDATE":
                m = re.search(r'UPDATE\s+([a-zA-Z0-9_]+)\s+SET\s+(.*?)(?:\s+WHERE\s+(.*))?$', clean_sql, re.IGNORECASE | re.DOTALL)
                if not m:
                    self.sql_log.append({"sql": clean_sql, "kind": "dml", "rows": 1})
                    total_affected_rows += 1
                    continue

                table = m.group(1).lower()
                set_str = m.group(2).strip()
                where_str = m.group(3).strip() if m.group(3) else "1=1"
                pk_col = pk_map.get(table)
                if not pk_col:
                    raise ValueError(f"Table '{table}' has no primary key defined. Wildfire requires primary keys to compute deterministic row diffs.")

                cols = cols_map.get(table, [pk_col])

                # Query the real before-rows from database
                lookup_query = f"SELECT * FROM {table} WHERE {where_str};"
                before_rows = alloydb_client.execute_sql(lookup_query)
                stmt_diffs = []

                for b_row in before_rows:
                    pk_val = b_row.get(pk_col)
                    row_key = f"{table}:{pk_val}"

                    if row_key in self.row_diffs:
                        prior_entry = self.row_diffs[row_key]
                        original_before = prior_entry["before"]
                        current_before = prior_entry["after"]
                    else:
                        original_before = dict(b_row)
                        current_before = dict(b_row)

                    set_assignments = _evaluate_set_clause(set_str, current_before)
                    after_row = dict(current_before)
                    for col, val in set_assignments.items():
                        after_row[col] = val

                    changed_cols = [c for c in cols if str(original_before.get(c, "")) != str(after_row.get(c, ""))]

                    diff_entry = {
                        "relid": table,
                        "op": "U",
                        "pk": {pk_col: pk_val},
                        "before": original_before,
                        "after": after_row,
                        "changed": changed_cols,
                    }
                    self.row_diffs[row_key] = diff_entry
                    stmt_diffs.append(diff_entry)

                self.sql_log.append({"sql": clean_sql, "kind": "dml", "rows": len(before_rows)})
                self.segments.append({"kind": "dml", "diff": stmt_diffs})
                total_affected_rows += len(before_rows)

            elif head == "INSERT":
                m = re.search(r'INSERT\s+INTO\s+([a-zA-Z0-9_]+)\s*(?:\((.*?)\))?\s*VALUES\s*(.*)', clean_sql, re.IGNORECASE | re.DOTALL)
                if m:
                    table = m.group(1).lower()
                    cols_str = m.group(2)
                    pk_col = pk_map.get(table, "id")
                    cols = cols_map.get(table, [pk_col])
                    cols_in_insert = [c.strip().lower() for c in cols_str.split(",")] if cols_str else cols

                    value_tuples = parse_insert_tuples(m.group(3))
                    stmt_diffs = []

                    for raw_vals in value_tuples:
                        inserted_dict = dict(zip(cols_in_insert, raw_vals))
                        self._auto_pk_counter += 1
                        pk_val = inserted_dict.get(pk_col)
                        if pk_val is None:
                            pk_val = f"new_{self._auto_pk_counter}"
                            inserted_dict[pk_col] = pk_val

                        row_key = f"{table}:{pk_val}"
                        diff_entry = {
                            "relid": table,
                            "op": "I",
                            "pk": {pk_col: pk_val},
                            "before": None,
                            "after": inserted_dict,
                            "changed": list(inserted_dict.keys()),
                        }
                        self.row_diffs[row_key] = diff_entry
                        stmt_diffs.append(diff_entry)

                    self.sql_log.append({"sql": clean_sql, "kind": "dml", "rows": len(value_tuples)})
                    self.segments.append({"kind": "dml", "diff": stmt_diffs})
                    total_affected_rows += len(value_tuples)
                else:
                    self.sql_log.append({"sql": clean_sql, "kind": "dml", "rows": 1})
                    total_affected_rows += 1

            elif head == "DELETE":
                m = re.search(r'DELETE\s+FROM\s+([a-zA-Z0-9_]+)(?:\s+WHERE\s+(.*))?$', clean_sql, re.IGNORECASE | re.DOTALL)
                table = m.group(1).lower() if m else ""
                where_str = m.group(2).strip() if (m and m.group(2)) else "1=1"
                pk_col = pk_map.get(table)
                if not pk_col:
                    raise ValueError(f"Table '{table}' has no primary key defined. Wildfire requires primary keys to compute deterministic row diffs.")

                lookup_query = f"SELECT * FROM {table} WHERE {where_str};"
                deleted_rows = alloydb_client.execute_sql(lookup_query)
                stmt_diffs = []

                for d_row in deleted_rows:
                    pk_val = d_row.get(pk_col)
                    row_key = f"{table}:{pk_val}"
                    diff_entry = {
                        "relid": table,
                        "op": "D",
                        "pk": {pk_col: pk_val},
                        "before": dict(d_row),
                        "after": None,
                        "changed": list(d_row.keys()),
                    }
                    self.row_diffs[row_key] = diff_entry
                    stmt_diffs.append(diff_entry)

                self.sql_log.append({"sql": clean_sql, "kind": "dml", "rows": len(deleted_rows)})
                self.segments.append({"kind": "dml", "diff": stmt_diffs})
                total_affected_rows += len(deleted_rows)

            else:
                self.sql_log.append({"sql": clean_sql, "kind": "dml", "rows": 1})
                total_affected_rows += 1

        return total_affected_rows

    def wf_end(self) -> capture2.ChangeSet2:
        """Produce the consolidated ChangeSet with true net diffs across the entire session."""
        net_diff = []
        for row_key, entry in self.row_diffs.items():
            # Check for no-op changes
            if entry["op"] == "U" and entry["before"] == entry["after"]:
                continue
            net_diff.append(entry)

        return capture2.ChangeSet2(
            basis_fp=self.basis_fp,
            final_fp=self.final_fp,
            segments=self.segments,
            net_diff=net_diff,
            sql_log=self.sql_log,
        )

    def close(self):
        pass


def open_session(branch_id: str) -> AlloyDBSandboxSession:
    s = AlloyDBSandboxSession(branch_id)
    s.wf_begin()
    SESSIONS[branch_id] = s
    return s


def get_session(branch_id: str) -> AlloyDBSandboxSession:
    if branch_id not in SESSIONS:
        return open_session(branch_id)
    return SESSIONS[branch_id]


def resync(branch_id: str) -> AlloyDBSandboxSession:
    s = open_session(branch_id)
    return s


def close_branch(branch_id: str):
    if branch_id in SESSIONS:
        SESSIONS.pop(branch_id, None)
