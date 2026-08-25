"""Multi-agent concurrency: two changesets captured from the SAME basis,
merged concurrently (threads). Three scenarios:

  S1 disjoint rows                 -> both merge
  S2 same rows, DIFFERENT columns  -> both merge, both edits preserved
  S3 same rows, SAME column        -> first merges, second data_conflict
"""
import threading
import db
import seed
import capture2
import appliers2

PRED_A = "id BETWEEN 1 AND 100"


def capture_on_fresh_sandbox(traj):
    """Re-seed the sandbox to the shared basis, run one agent, capture."""
    conn = db.connect(db.SANDBOX_DSN)
    seed.seed(conn)
    conn.close()
    s = capture2.SandboxSession2()
    s.wf_begin()
    for sql in traj:
        s.execute(sql)
    cs = s.wf_end()
    s.close()
    return cs


def merge_concurrently(cs_a, cs_b):
    results = {}

    def go(name, cs):
        results[name] = appliers2.apply_segmented(cs)

    ta = threading.Thread(target=go, args=("A", cs_a))
    tb = threading.Thread(target=go, args=("B", cs_b))
    ta.start(); tb.start(); ta.join(); tb.join()
    return results


def scenario(title, traj_a, traj_b, check=None):
    seed.reset_both()
    cs_a = capture_on_fresh_sandbox(traj_a)
    cs_b = capture_on_fresh_sandbox(traj_b)   # same basis as A
    rs = merge_concurrently(cs_a, cs_b)
    outcomes = sorted((n, r.outcome) for n, r in rs.items())
    extra = check() if check else ""
    print(f"{title}: {outcomes} {extra}")


def both_edits_survive():
    conn = db.connect(db.PRIMARY_DSN)
    r = db.rows(conn, "SELECT tax, comment FROM orders WHERE id = 60")[0]
    conn.close()
    return f"| row60 tax={r['tax']} comment={r['comment']!r}"


if __name__ == "__main__":
    scenario("S1 disjoint rows",
             [f"UPDATE orders SET tax = tax + 1, price = price - 1 WHERE {PRED_A}"],
             ["UPDATE orders SET tax = tax + 1, price = price - 1 WHERE id BETWEEN 101 AND 200"])

    scenario("S2 same rows, different columns",
             [f"UPDATE orders SET tax = tax + 1, price = price - 1 WHERE {PRED_A}"],
             ["UPDATE orders SET comment = 'audited' WHERE id BETWEEN 50 AND 150"],
             check=both_edits_survive)

    scenario("S3 same rows, same column",
             [f"UPDATE orders SET tax = tax + 1, price = price - 1 WHERE {PRED_A}"],
             ["UPDATE orders SET tax = 7.00 WHERE id BETWEEN 50 AND 150"])
