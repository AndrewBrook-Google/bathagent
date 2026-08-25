"""Eval runner: case x option matrix.

Per run:
  1. reset primary+sandbox to identical seed state
  2. scripted agent works in the sandbox (wf_begin .. wf_end) -> ChangeSet
  3. validator checks the diff (bounds / scope / invariant)
  4. drift is injected on the PRIMARY (the world moves on)
  5. apply via Option 1 (replay SQL) or Option 2 (apply diff)
  6. verify: primary vs approved sandbox state, drift survival, undo
"""
import json
import db
import seed
import capture
import appliers

TARGET_PREDICATE = """product_id IN (SELECT id FROM products
    WHERE category='toothbrush' AND imported)
  AND country='Elbonia' AND ordered_at >= '2026-01-01'"""

BASELINE_TRAJECTORY = [
    # exploratory noise a real agent produces
    ("query", "SELECT count(*) FROM orders WHERE " + TARGET_PREDICATE),
    # a wrong step + self-correction (net effect: tax +1, price -1)
    ("execute", "UPDATE orders SET tax = tax + 2 WHERE " + TARGET_PREDICATE),
    ("execute", "UPDATE orders SET tax = tax - 1, price = price - 1 WHERE " + TARGET_PREDICATE),
    ("execute", """UPDATE orders SET comment =
        'Tax adjusted per Elbonia import law (+$1); price reduced to keep total unchanged.'
        WHERE """ + TARGET_PREDICATE),
]

NONDET_TRAJECTORY = [
    ("execute", """UPDATE orders SET
        tax = tax + 1, price = price - 1,
        comment = 'REF-' || substr(md5(random()::text), 1, 8) || ': Elbonia tax adjustment'
        WHERE """ + TARGET_PREDICATE),
]

CASES = [
    {"id": "C1_baseline_no_drift", "trajectory": BASELINE_TRAJECTORY,
     "drift": [], "desc": "formula update, quiet world (approval wait 1.5s)"},
    {"id": "C2_drift_same_column", "trajectory": BASELINE_TRAJECTORY,
     "drift": ["UPDATE orders SET tax = 9.00, updated_by = 'ops_team' WHERE id = 42"],
     "desc": "row 42's tax changed on primary between capture and apply"},
    {"id": "C3_nondeterministic_sql", "trajectory": NONDET_TRAJECTORY,
     "drift": [], "desc": "trajectory uses random(); replay != approved preview"},
]

APPROVAL_DELAY = 1.5  # simulated human/LLM approval latency (seconds)


def run_agent(trajectory):
    s = capture.SandboxSession()
    s.wf_begin()
    for kind, sql in trajectory:
        getattr(s, kind)(sql)
    cs = s.wf_end()
    s.close()
    return cs


def validate(cs):
    """Deterministic legality checks on the static diff."""
    checks = {}
    checks["rowcount<=5000"] = len(cs.diff) <= 5000
    delta = sum(float(d["after"]["total"]) - float(d["before"]["total"])
                for d in cs.diff if d["op"] == "U")
    checks["sum(total) unchanged"] = abs(delta) < 1e-9
    conn = db.connect(db.SANDBOX_DSN)
    in_scope = {r["id"] for r in db.rows(
        conn, "SELECT id FROM wf_snap_orders WHERE " + TARGET_PREDICATE)}
    conn.close()
    touched = {d["pk"]["id"] for d in cs.diff}
    checks["touched ⊆ read-scope"] = touched <= in_scope
    return checks


def verify(cs, drift_sqls):
    """Compare primary against the approved (sandbox) after-state."""
    conn = db.connect(db.PRIMARY_DSN)
    pks = [d["pk"]["id"] for d in cs.diff]
    # read via to_jsonb so both sides of the comparison went through the
    # same jsonb normalization (numerics, timestamp rendering)
    current = {r["j"]["id"]: r["j"] for r in db.rows(
        conn, "SELECT to_jsonb(o) AS j FROM orders o WHERE id = ANY(%s)", (pks,))}
    divergent = 0
    for d in cs.diff:
        row = current.get(d["pk"]["id"])
        for c in d["changed"]:
            if row is None or row.get(c) != d["after"][c]:
                divergent += 1
                break
    drift_survived = None
    if drift_sqls:  # did ops_team's concurrent edit survive?
        r = db.rows(conn, "SELECT tax, updated_by FROM orders WHERE id = 42")[0]
        drift_survived = (r["updated_by"] == "ops_team" and float(r["tax"]) == 9.0)
    conn.close()
    return divergent, drift_survived


def main():
    results = []
    for case in CASES:
        for option, apply_fn in (("option1", appliers.apply_replay_sql),
                                 ("option2", appliers.apply_diff)):
            seed.reset_both()
            cs = run_agent(case["trajectory"])
            checks = validate(cs)

            pconn = db.connect(db.PRIMARY_DSN)
            for sql in case["drift"]:
                db.run(pconn, sql)
            pconn.commit()
            pconn.close()

            res = apply_fn(cs, approval_delay=APPROVAL_DELAY)
            divergent, drift_survived = verify(cs, case["drift"])

            undo_ok = None
            if option == "option2" and res.outcome == "applied":
                undo_res = appliers.undo_diff(cs)
                d2, _ = verify(cs, [])
                undo_ok = (undo_res.outcome == "applied" and d2 == len(cs.diff))

            results.append({
                "case": case["id"], **res.as_dict(),
                "capture": cs.summary(), "validator": checks,
                "divergent_rows": divergent, "drift_survived": drift_survived,
                "undo_ok": undo_ok,
            })
            print(f"[{case['id']} / {option}] {res.outcome} "
                  f"applied={res.applied_rows} lock={res.lock_window_ms:.0f}ms "
                  f"divergent={divergent} drift_survived={drift_survived} undo={undo_ok}")

    with open("../eval/results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nwrote ../eval/results.json")


if __name__ == "__main__":
    main()
