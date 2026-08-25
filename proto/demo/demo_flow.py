"""CymbalAir demo: four agents, full event flow against the real merge engine.

  Act 1  Wingman-1 & Wingman-2 (booking role) — same basis, seat contention:
         W1 splits a family 2+2 across two flights -> LLM auto-approve -> merged
         W2 books the same seat 12A            -> approved but DATA_CONFLICT
         W2 resyncs, books 14C instead          -> merged
  Act 2  Developer agent — loyalty feature (ADD COLUMN + backfill + new table)
         -> deterministic rules pass, DDL forces HUMAN review -> approved -> merged
  Act 3  Admin agent — first proposal touches seats (outside role scope)
         -> deterministically REJECTED (never reaches LLM)
         GDPR anonymization of pre-2024-08 bookings -> LLM approve -> merged
         wrong cutoff! -> auto-created revert action -> merged, names restored

Run `python3 demo_flow.py` — starts from a fresh world (reset_demo) each time.
"""
import os
import pathlib
import sys

os.environ["WF_DBNAME"] = "cymbalair"
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "harness"))

import db            # noqa: E402
import capture2      # noqa: E402
import appliers2     # noqa: E402
import seed_air      # noqa: E402
import policy        # noqa: E402
import reviewer      # noqa: E402

STEP = [0]
FAILURES = []


def ev(actor, msg):
    STEP[0] += 1
    print(f"  {STEP[0]:>2}. [{actor:<10}] {msg}")


def expect(cond, what):
    if not cond:
        FAILURES.append(what)
        print(f"      !! EXPECTATION FAILED: {what}")


def agent_session(actor, task, traj):
    ev(actor, f"checkout branch (resync sandbox from primary)")
    seed_air.resync_sandbox()
    s = capture2.SandboxSession2()
    s.wf_begin()
    for sql in traj:
        n = s.execute(sql)
        ev(actor, f"sandbox: {sql.strip().splitlines()[0][:76]}… ({n} rows)" if len(sql) > 76
           else f"sandbox: {sql.strip()} ({n} rows)")
    cs = s.wf_end()
    s.close()
    ev(actor, f"propose changeset {cs.action_id[:8]} — {cs.summary()}")
    return cs


def review_and_merge(actor, role, task, cs, human_verdict="approved"):
    lane, checks = policy.route(cs, role)
    if lane == "reject":
        ev("validator", f"REJECT (deterministic, no LLM needed): {checks}")
        return "rejected"
    ev("validator", f"deterministic rules pass -> route to {lane.upper()}")
    if lane == "llm":
        r = reviewer.llm_review(cs, task, [f"role={role}"] )
        ev("llm-review", f"{r['verdict'].upper()}: {r['reason'][:90]}")
        if r["verdict"] != "approve":
            lane = "human"
    if lane == "human":
        r = reviewer.llm_review(cs, task, [f"role={role}"])
        ev("llm-review", f"pre-screen for human: {r['reason'][:80]}")
        ev("human", f"reviewed rendered diff + trajectory -> {human_verdict.upper()}")
        if human_verdict != "approved":
            return "rejected"
    res = appliers2.apply_segmented(cs)
    ev("merge-eng", f"{res.outcome.upper()} rows={res.applied_rows} "
                    f"lock={res.lock_window_ms:.0f}ms {res.detail}")
    return res.outcome


def main():
    print("=== CymbalAir demo — fresh world ===")
    seed_air.reset_demo()

    # ---------------- Act 1: two booking agents, seat contention ----------
    print("\n--- Act 1: Wingman-1 vs Wingman-2 (same basis, seat 12A) ---")
    task1 = "Family of 4 on cancelled CA-455: rebook tonight, split 2+2 if needed"
    cs_w1 = agent_session("wingman-1", task1, [
        "INSERT INTO bookings (id, flight_id, passenger, seat_no, price) VALUES"
        " ('BK-W1-ALICE', 789, 'Alice', '12A', 189.00),"
        " ('BK-W1-CATE',  789, 'Cate',  '12B', 189.00),"
        " ('BK-W1-BOB',   320, 'Bob',   '21A', 175.00),"
        " ('BK-W1-DAN',   320, 'Dan',   '21B', 175.00)",
        "UPDATE seats SET status='booked', booking_id='BK-W1-ALICE' WHERE id=1",
        "UPDATE seats SET status='booked', booking_id='BK-W1-CATE'  WHERE id=2",
        "UPDATE seats SET status='booked', booking_id='BK-W1-BOB'   WHERE id=5",
        "UPDATE seats SET status='booked', booking_id='BK-W1-DAN'   WHERE id=6",
    ])
    task2 = "Book Erin on the next flight to MCO tonight"
    cs_w2 = agent_session("wingman-2", task2, [   # captured BEFORE w1 merges: same basis
        "INSERT INTO bookings (id, flight_id, passenger, seat_no, price) VALUES"
        " ('BK-W2-ERIN', 789, 'Erin', '12A', 189.00)",
        "UPDATE seats SET status='booked', booking_id='BK-W2-ERIN' WHERE id=1",
    ])
    # NOTE: w2's checkout happened before w1's merge (agent_session resyncs, and
    # w1 hasn't merged yet) — both changesets share the same basis. Now merge:
    out = review_and_merge("wingman-1", "booking", task1, cs_w1)
    expect(out == "applied", "w1 merges cleanly")
    out = review_and_merge("wingman-2", "booking", task2, cs_w2)
    expect(out == "data_conflict", "w2 hits data_conflict on seat 12A")
    ev("wingman-2", "conflict on seat 12A -> resync, pick another seat")
    cs_w2b = agent_session("wingman-2", task2, [
        "INSERT INTO bookings (id, flight_id, passenger, seat_no, price) VALUES"
        " ('BK-W2-ERIN', 789, 'Erin', '14C', 189.00)",
        "UPDATE seats SET status='booked', booking_id='BK-W2-ERIN' WHERE id=3",
    ])
    out = review_and_merge("wingman-2", "booking", task2, cs_w2b)
    expect(out == "applied", "w2 retry merges")

    # ---------------- Act 2: developer agent, DDL escalates to human ------
    print("\n--- Act 2: developer agent — loyalty feature (DDL+DML) ---")
    task3 = "New feature: loyalty points; backfill from booking price"
    cs_dev = agent_session("developer", task3, [
        "ALTER TABLE bookings ADD COLUMN loyalty_points int",
        "UPDATE bookings SET loyalty_points = floor(price)::int",
        "CREATE TABLE loyalty_accounts (passenger text PRIMARY KEY, points int NOT NULL)",
        "INSERT INTO loyalty_accounts SELECT passenger, sum(floor(price))::int"
        " FROM bookings GROUP BY passenger",
    ])
    out = review_and_merge("developer", "developer", task3, cs_dev)
    expect(out == "applied", "developer changeset merges after human approval")

    # ---------------- Act 3: admin agent — reject, GDPR, revert -----------
    print("\n--- Act 3: admin agent — scope reject, GDPR cleanup, revert ---")
    task4 = "Data hygiene sweep"
    cs_bad = agent_session("db-admin", task4, [
        "UPDATE seats SET cabin = upper(cabin)",   # outside admin scope!
    ])
    out = review_and_merge("db-admin", "admin", task4, cs_bad)
    expect(out == "rejected", "admin out-of-scope proposal rejected deterministically")
    ev("db-admin", "proposal discarded; resync and do the actual GDPR task")

    task5 = "GDPR retention: anonymize passenger names on bookings older than 2 years"
    cs_gdpr = agent_session("db-admin", task5, [
        "UPDATE bookings SET passenger = 'REDACTED-' || md5(id)"
        " WHERE created_at < '2024-08-02' AND status = 'cancelled'",
    ])
    out = review_and_merge("db-admin", "admin", task5, cs_gdpr)
    expect(out == "applied", "GDPR changeset merges")

    ev("db-admin", "cutoff was wrong (policy says 3 years, not 2) -> request revert")
    rc = appliers2.revert_changeset(cs_gdpr)
    ev("merge-eng", f"auto-created revert action {rc.action_id[:8]} (reverts {rc.reverts[:8]})")
    res = appliers2.apply_segmented(rc)
    ev("merge-eng", f"revert {res.outcome.upper()} rows={res.applied_rows}")
    conn = db.connect(db.PRIMARY_DSN)
    names_back = db.rows(conn, "SELECT count(*) n FROM bookings WHERE passenger IN "
                               "('Grace Hopper','Alan Kay','Barbara Liskov')")[0]["n"]
    conn.close()
    expect(res.outcome == "applied" and names_back == 3, "revert restores all 3 names")

    # ---------------- wrap-up ---------------------------------------------
    print("\n=== final state checks ===")
    conn = db.connect(db.PRIMARY_DSN)
    for q, label in [
        ("SELECT count(*) n FROM bookings WHERE id LIKE 'BK-W%'", "wingman bookings"),
        ("SELECT count(*) n FROM seats WHERE status='booked'", "booked seats"),
        ("SELECT count(*) n FROM loyalty_accounts", "loyalty accounts"),
        ("SELECT count(*) n FROM wf_applied", "merged actions (audit)"),
    ]:
        print(f"  {label}: {db.rows(conn, q)[0]['n']}")
    conn.close()
    if FAILURES:
        print(f"\nDEMO BROKE: {FAILURES}")
        sys.exit(1)
    print("\nall expectations met ✅  (rerun from scratch: python3 demo_flow.py)")


if __name__ == "__main__":
    main()
