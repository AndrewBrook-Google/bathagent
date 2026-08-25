# Scoop merge-engine prototype — design record

Status: PoC validated 2026-08-01/02. Code under `proto/`; all claims below
are backed by runnable evals (`harness/run_eval*.py`, `harness/test_concurrent.py`,
`demo/demo_flow.py`).

## Settled decisions

1. **The diff is the contract.** Review approves a static artifact (net row
   diff + sql_log as intent evidence); the merge engine enforces it
   deterministically. **No LLM anywhere in the merge path.**
2. **ChangeSet = epoch-segmented capture.** Every DDL closes the current DML
   epoch (incremental diff) and records before/after schema fingerprints.
   Segments preserve the agent's ordering — required because DDL/DML
   dependencies go both ways (ADD COLUMN→backfill vs backfill→SET NOT NULL).
3. **Two apply paths, one contract.**
   - M1 segmented apply (default): DDL = fp-check→replay→fp-check;
     DML = stage→ordered locks→before-image check (changed cols only)→key-join.
   - M2 execute-verify (for new-row coverage): replay sql_log, recompute net
     diff, strict compare incl. before-images, mismatch→ROLLBACK. Rejects
     nondeterministic SQL structurally; router must pre-screen volatile fns.
4. **Schema fingerprint = DDL's before-image.** md5 over columns+indexes+
   constraints; checked at basis and around every DDL segment.
5. **Column-level drift tolerance.** Conflict = same row AND same column
   drifted. Concurrent edits to other rows/columns merge cleanly (verified:
   test_concurrent.py S1–S3). Known hole: cross-column invariants can be
   torn by two column-disjoint merges — mitigate with merge-time assertion
   hooks and (future) policy column-groups.
6. **Merges are globally serialized** via pg_advisory_xact_lock (merge
   queue). Removes the DDL-fingerprint TOCTOU; cost negligible at ms-scale
   windows. Relax to per-DB/per-table locks only if throughput demands.
7. **Failure taxonomy** (all full-txn rollback, zero residue):
   `schema_drift` (resync only) | `data_conflict` (resync+replay; row-level
   blast radius) | `verify_mismatch` (drift→resync; nondet→rewrite traj)
   | `error` (FK/timeout/dup-idempotency-key; retry safe).
8. **Idempotency gate**: `wf_applied(action_id)` PK; unknown-outcome retries
   are safe.
9. **Revert = best-effort compensating changeset** (swap before/after,
   invert ops, new action_id, audit-chained via `reverts`). Same engine,
   same OCC guards: clean rows restore, subsequently-edited rows
   data_conflict (verified). DML-only; DDL changesets have no mechanical
   revert. Positioning: hot undo button, not a time machine; does not undo
   external side effects (nothing does, incl. PITR).
10. **Conflict-vs-latency tradeoff**: review is offline (blocks nothing;
    staleness ↑ conflict probability), merge is blocking but ms-scale.
    Resync+mechanical sql_log replay makes redo cheap; full rebase
    (diff-of-diffs + delta re-review) designed but deferred.
11. **Review routing ladder** (demo/policy.py): admin-authored role
    templates → deterministic checks (scope/ceiling/ddl) → reject | LLM
    (small, no DDL) | human (DDL always). LLM = Vertex Gemini
    (WF_LLM=vertex) with canned fallback for offline replay.
12. **INSERT identity**: client-generated ids (text/UUID) — demo uses them.

## Open items (deliberately deferred)

- read-set capture (needed for real scope checks + rebase-safety classification)
- basis TTL / forced-resync policy
- chunked apply for >1-txn changesets (C6a compensation via before-images)
- identity separation validator vs agent (user decision: later)
- CREATE INDEX CONCURRENTLY segment type; multi-col PK; no-PK refusal UX
- rebase fast-path (resync + mechanical replay + diff-of-diffs delta review)
- capture efficiency at scale (snapshot-EXCEPT → transition-table triggers)

## Layout

- `harness/` db.py seed.py capture.py appliers.py run_eval.py (v1: SQL-replay
  vs diff-apply), capture2.py appliers2.py run_eval2.py (v2: DDL+DML,
  14/14), test_concurrent.py (S1–S3)
- `demo/` CymbalAir CUJ: schema_air.sql seed_air.py (reset_demo = one-shot
  restore) policy.py reviewer.py demo_flow.py (4 agents, 49-step event flow)
- `eval/` REPORT.md REPORT2.md results*.json
- containers: wf-primary :55432, wf-sandbox :55433 (postgres:15);
  WF_DBNAME selects bathstuff (evals) / cymbalair (demo)
