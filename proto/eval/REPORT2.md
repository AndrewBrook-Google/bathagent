# Merge engine v2 — mixed DDL+DML changesets, two apply paths

Run date: 2026-08-02. 7 cases × {M1 segmented apply, M2 execute-verify},
**14/14 outcomes as expected**. Raw data: `results2.json`.

## Pipeline design

Capture produces an **epoch-segmented ChangeSet**: every DDL statement
closes the current DML epoch (incremental row diff), records its own
segment with before/after **schema fingerprints** (md5 over columns +
indexes + constraints), and refreshes snapshots. The changeset carries:

- `segments` — ordered `[dml_diff₀][ddl₁][dml_diff₁]…` (M1's input)
- `net_diff` — basis vs final state (the merge contract; M2's comparand)
- `sql_log` — full ordered statement log (M2's input; reviewer evidence)
- `basis_fp` / `final_fp` — schema fingerprints (drift guard both ends)

**M1 segmented apply**: one txn; basis-fingerprint guard + `wf_applied`
idempotency gate; then per segment — DDL: fp-check → replay → fp-check;
DML: stage → lock (ordered) → before-image conflict check → key-join apply.

**M2 execute-verify**: one txn; same guards; replay the whole sql_log;
recompute the net diff on the primary; strict set-compare against the
approved contract **including before-images** (catches "same end state but
clobbered a concurrent edit"); compare final schema fingerprint;
any mismatch → ROLLBACK.

## Results

| Case | M1 segmented | M2 execute-verify |
|---|---|---|
| X1 pure DML (incl. agent self-correction noise) | ✅ applied, div 0 | ✅ applied, div 0 |
| X2 ADD COLUMN → backfill (DDL before DML) | ✅ applied | ✅ applied |
| X3 backfill → SET NOT NULL (DML before DDL) | ✅ applied — **proves order-preserving segments; any "DDL-first" scheme fails here** | ✅ applied |
| X4 CREATE TABLE + INSERT + UPDATE existing | ✅ applied (new table = all-inserts diff) | ✅ applied |
| X5 data drift on target row | ✅ **data_conflict** (before-image, row 42) | ✅ **verify_mismatch** (1 approved-only / 1 replay-only) |
| X6 schema drift (DBA added a column on primary) | ✅ **schema_drift** at basis fp | ✅ **schema_drift** at basis fp |
| X7 `random()` in SQL | ✅ applied (diff is exact) | ✅ **verify_mismatch** 200/200 — M2 structurally rejects nondeterministic SQL |
| Idempotency: replay same changeset | ✅ blocked by `wf_applied` PK | (same gate) |

All applied cases: primary end-state == sandbox after-state (0 divergent
rows), final schema fingerprint matches. Lock windows 8–84 ms at this scale.

## What this settles

1. **Mixed DDL+DML works in one atomic merge txn** (PG DDL is transactional).
   Order preservation via epochs is the load-bearing design decision (X3).
2. **Schema fingerprint = the DDL analog of the row before-image.** Same
   protocol shape at both levels; X6 shows both paths abort cleanly.
3. **M1 vs M2 division of labor confirmed**: X7 shows M2 cannot carry
   nondeterministic SQL (feature: it forces determinism, but router must
   know); X5 shows both catch drift, M1 with a precise row-level message,
   M2 with a coarser diff-set message. M1 remains the default; M2 is the
   path for C4d (new-row coverage) — not yet in this matrix.

## Known gaps / next

- C4d case for M2 (new rows arriving during isolation — its raison d'être)
- CREATE INDEX CONCURRENTLY (cannot run in txn — needs its own segment type)
- fingerprint scope is whole-schema: unrelated DDL elsewhere blocks merge
  (conservative; per-table scoping is a tuning knob)
- DDL-containing changesets have no undo path (row-diff undo only)
- multi-column PKs, no-PK tables refused (by design, pitfall ⑤)
