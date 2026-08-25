"""Eval v2: mixed DDL+DML changesets x {M1 segmented, M2 execute-verify}."""
import json
import db
import seed
import capture2
import appliers2

PRED = """product_id IN (SELECT id FROM products
    WHERE category='toothbrush' AND imported)
  AND country='Elbonia' AND ordered_at >= '2026-01-01'"""

CASES = [
    {"id": "X1_pure_dml",
     "traj": [
         "UPDATE orders SET tax = tax + 2 WHERE " + PRED,          # wrong step
         "UPDATE orders SET tax = tax - 1, price = price - 1 WHERE " + PRED,
     ],
     "drift": [],
     "expect": {"M1_segmented": "applied", "M2_exec_verify": "applied"}},

    {"id": "X2_add_column_backfill",       # DDL before DML
     "traj": [
         "ALTER TABLE orders ADD COLUMN tax_note text",
         "UPDATE orders SET tax = tax + 1, price = price - 1, "
         "tax_note = 'Elbonia import tax +$1' WHERE " + PRED,
     ],
     "drift": [],
     "expect": {"M1_segmented": "applied", "M2_exec_verify": "applied"}},

    {"id": "X3_backfill_then_not_null",    # DML before DDL — order dependency
     "traj": [
         "UPDATE orders SET comment = 'n/a' WHERE comment IS NULL",
         "ALTER TABLE orders ALTER COLUMN comment SET NOT NULL",
     ],
     "drift": [],
     "expect": {"M1_segmented": "applied", "M2_exec_verify": "applied"}},

    {"id": "X4_create_table_insert",       # new table + DML on existing table
     "traj": [
         "CREATE TABLE order_audit (id bigint PRIMARY KEY, order_id bigint "
         "REFERENCES orders(id), note text)",
         "INSERT INTO order_audit SELECT id, id, 'tax adjusted' FROM orders WHERE " + PRED,
         "UPDATE orders SET tax = tax + 1, price = price - 1 WHERE " + PRED,
     ],
     "drift": [],
     "expect": {"M1_segmented": "applied", "M2_exec_verify": "applied"}},

    {"id": "X5_data_drift",                # concurrent edit on a target row
     "traj": [
         "ALTER TABLE orders ADD COLUMN tax_note text",
         "UPDATE orders SET tax = tax + 1, price = price - 1, "
         "tax_note = 'Elbonia import tax +$1' WHERE " + PRED,
     ],
     "drift": ["UPDATE orders SET tax = 9.00, updated_by = 'ops_team' WHERE id = 42"],
     "expect": {"M1_segmented": "data_conflict", "M2_exec_verify": "verify_mismatch"}},

    {"id": "X6_schema_drift",              # DBA altered primary during isolation
     "traj": [
         "UPDATE orders SET tax = tax + 1, price = price - 1 WHERE " + PRED,
     ],
     "drift": ["ALTER TABLE orders ADD COLUMN oops text"],
     "expect": {"M1_segmented": "schema_drift", "M2_exec_verify": "schema_drift"}},

    {"id": "X7_nondeterministic_sql",
     "traj": [
         "UPDATE orders SET tax = tax + 1, price = price - 1, "
         "comment = 'REF-' || substr(md5(random()::text),1,8) WHERE " + PRED,
     ],
     "drift": [],
     "expect": {"M1_segmented": "applied", "M2_exec_verify": "verify_mismatch"}},
]

APPROVAL_DELAY = 0.0   # C5 already demonstrated in v1; keep v2 runs fast


def run_agent(traj):
    s = capture2.SandboxSession2()
    s.wf_begin()
    for sql in traj:
        s.execute(sql)
    cs = s.wf_end()
    s.close()
    return cs


def verify_final_state(cs):
    """After an 'applied' merge, primary must equal the sandbox after-state."""
    conn = db.connect(db.PRIMARY_DSN)
    divergent = 0
    tables = sorted({d["relid"] for d in cs.net_diff})
    for t in tables:
        pk = capture2.table_pk(conn, t)
        pks = [d["pk"][pk] for d in cs.net_diff if d["relid"] == t]
        current = {r["j"][pk]: r["j"] for r in db.rows(
            conn, f"SELECT to_jsonb(o) AS j FROM {t} o WHERE {pk} = ANY(%s)", (pks,))}
        for d in cs.net_diff:
            if d["relid"] != t:
                continue
            row = current.get(d["pk"][pk])
            for c in d["changed"]:
                if row is None or row.get(c) != (d["after"] or {}).get(c):
                    divergent += 1
                    break
    fp_ok = capture2.schema_fingerprint(conn) == cs.final_fp
    conn.close()
    return divergent, fp_ok


def main():
    results = []
    for case in CASES:
        for mode, fn in (("M1_segmented", appliers2.apply_segmented),
                         ("M2_exec_verify", appliers2.apply_execute_verify)):
            seed.reset_both()
            cs = run_agent(case["traj"])

            pconn = db.connect(db.PRIMARY_DSN)
            for sql in case["drift"]:
                db.run(pconn, sql)
            pconn.commit()
            pconn.close()

            res = fn(cs, approval_delay=APPROVAL_DELAY)
            divergent = fp_ok = None
            if res.outcome == "applied":
                divergent, fp_ok = verify_final_state(cs)
            ok = res.outcome == case["expect"][mode] and (divergent in (0, None))
            results.append({"case": case["id"], **res.as_dict(),
                            "expected": case["expect"][mode], "as_expected": ok,
                            "divergent_rows": divergent, "schema_ok": fp_ok,
                            "capture": cs.summary()})
            mark = "✅" if ok else "❌"
            print(f"{mark} [{case['id']} / {mode}] {res.outcome} "
                  f"(expect {case['expect'][mode]}) rows={res.applied_rows} "
                  f"lock={res.lock_window_ms:.0f}ms div={divergent} {res.detail[:80]}")

    with open("../eval/results2.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    bad = [r for r in results if not r["as_expected"]]
    print(f"\n{len(results) - len(bad)}/{len(results)} as expected; wrote ../eval/results2.json")


if __name__ == "__main__":
    main()
