# Scoop prototype — Option 1 (replay SQL) vs Option 2 (apply diff)

Run date: 2026-08-01. Local setup: two `postgres:15` docker containers
(`wf-primary` :55432, `wf-sandbox` :55433), 500 orders (200 in the target
set), simulated approval latency 1.5 s, drift injected on the primary
between capture and apply. Both options consume the SAME agent session's
ChangeSet `{sql_log, diff}`; only the apply path differs.

## Results (3 cases × 2 options)

| Case | Option | Outcome | Rows touched | Primary lock window | Divergence from approved state | Concurrent edit survived? | Undo |
|---|---|---|---|---|---|---|---|
| C1 baseline, no drift | 1 replay SQL | applied | **600** (replays agent noise) | **1510 ms** (holds txn across approval) | 0 | — | n/a |
| C1 baseline, no drift | 2 apply diff | applied | 200 | **38 ms** | 0 | — | ✅ reverts cleanly |
| C2 drift on same column | 1 replay SQL | applied | 600 | 1508 ms | **1 row silently wrong** (row 42: tax=10, approved 5) | ❌ ops_team edit destroyed | n/a |
| C2 drift on same column | 2 apply diff | **conflict_abort** | 0 | 51 ms | — (correctly refused) | ✅ preserved | n/a |
| C3 `random()` in SQL | 1 replay SQL | applied | 200 | 1504 ms | **200/200 rows differ from approved preview** | — | n/a |
| C3 `random()` in SQL | 2 apply diff | applied | 200 | 46 ms | 0 | — | ✅ |

Validator (deterministic, on the static diff): rowcount ceiling,
`sum(total)` invariant, touched ⊆ read-scope — all pass on all captures.

Capture stats: 3 SQL statements / 200-row diff / ~105 KB JSON.

## What this confirms from RFC3

- **C5 (adjudicate offline) is the dominant axis.** Option 1's primary lock
  window == approval latency (1.5 s here; hours with a human). Option 2's
  window is ~40 ms regardless of approval latency.
- **C4b for free.** The before-image check caught the concurrent edit;
  replay silently destroyed it (row 42: replayed `tax+2-1` on top of the
  drifted value 9 → 10, neither the approved 5 nor ops_team's 9).
- **C4a.** `random()` in the trajectory: replay produced 200/200 rows that
  differ from what the reviewer approved. Diff-apply is exact.
- **Noise amplification.** The agent's wrong-step + self-correction is
  replayed verbatim by Option 1 (600 row-touches for a 200-row net change);
  the diff captures net effect only.
- **C6b.** Undo (before/after swapped through the same apply path) verified.

## Not yet covered (next cases to add)

- C4d new-rows-after-snapshot (Option 1's one real advantage) + re-scan mitigation
- INSERT identity collision, DELETE, triggers double-fire, no-PK table refusal
- Scale (500 k rows): chunked apply, lock-window probe under concurrent writers
- Crash-mid-apply + idempotency gate (`wf_applied`)
- LLM agent for bespoke per-row values (SQL degenerates into VALUES list)

## Layout

- `proto/harness/capture.py` — wf_begin/wf_end, snapshot-EXCEPT diff
- `proto/harness/appliers.py` — the two apply paths + undo
- `proto/harness/run_eval.py` — case × option matrix
- `proto/eval/results.json` — raw run output
