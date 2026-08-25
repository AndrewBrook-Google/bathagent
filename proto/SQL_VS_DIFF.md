# Wildfire write-back: Propose-SQL vs State-Diff

Status: draft for discussion, 2026-08-09. Distills go/wildfire-rfc3 §4
(Option 1 vs Option 2) plus the empirical evidence accumulated since:
three adversarial QA rounds against the state-diff engine
(`proto/prodpoc/`, 92-case regression matrix, all claims runnable).

Context: [Concept: Wildfire (rfc1)] · [Project Scoop (rfc2)] ·
[rfc3: Change Capture, Validation and Re-apply] · `proto/DESIGN.md`

---

## 0. TL;DR

The two designs are not symmetric alternatives. Once "the service somehow
checks the SQL" is forced to be concrete, it collapses into either weak
static checks or execute-to-validate — and the latter is the state-diff
design executed in the worst possible place (an open transaction on the
primary). The honest conclusion is not "pick one" but a **routing table**:

| CUJ profile | Path |
|---|---|
| Interactive, small-N, machine-instant deterministic rules, identity-scoped (Wingman-style) | **A: Propose-SQL fast path** |
| Bulk / DDL / human-or-LLM approval in the loop / undo required / high risk tier (Toothbrush-style) | **B: State-diff trunk** |
| Bulk formulaic changes | **B executes, SQL attached as review evidence** (rfc3 "advanced variant") |

Routing variables: **reviewer latency**, **change size**, **speculative or
not**, **concurrency scale**. Reviewer latency dominates (§4, caveat ①).

---

## 1. The two designs

**Path A — Propose-SQL.** The client (agent or app) submits SQL text. The
service validates it — AST allowlist plus, where rules need the effect,
execute→assert→rollback/commit in a transaction — and executes it on the
primary under the merge caller's EUC.

**Path B — Sandbox state-diff.** The agent works freely against a replica.
The engine passively records first-touch before-images (statement triggers)
and per-DDL catalog frames (event trigger); at propose time it captures a
static artifact `{basis_fp, final_fp, schema_delta, data_delta, stats}`,
which is reviewed offline and applied by engine-generated SQL in one short
transaction. No agent SQL text is ever captured or executed on the primary.
(Capture pipeline: Appendix B.)

---

## 2. Core framing: validate the program vs validate the state change

Path A's load-bearing wall is "somehow check". Expanded, there are exactly
two mechanisms:

**Static checks on SQL text** (parse-tree allowlist, no-DDL, volatile-fn
scan, EXPLAIN row estimates). SQL text severely under-determines semantics.
This is not theoretical — the QA rounds hit a whole family of
session-state attacks where the statement text is unchanged but the effect
is not:

- `search_path`: a bare identifier `line` binds to `pg_catalog.line`, not
  the user table (matrix G07);
- `extra_float_digits = -3` silently truncates five digits off every
  float8 (H10);
- `session_replication_role = 'replica'` silences ORIGIN triggers **and
  disables FK enforcement** (H13);
- `lc_monetary` rewrites `money` rendering.

A text-level validator is blind to all of these. EXPLAIN gives the
planner's guess, which can be off by orders of magnitude (rfc3 §5:
execution plan ≠ data diff).

**Execute-to-validate.** Then rfc3 §3.3.1 applies: the check sits between
execute and commit, holding locks on the primary. A human or LLM in the
approval path is minutes-to-hours → the transaction cannot be held →
two-pass execution → **approved ≠ executed**. This was the reason Option 1
was scored a strawman in rfc3, and it still holds.

So Path A is not an alternative to Path B; it is Path B **minus isolation,
minus before-images, minus offline adjudication** — the only real question
is where the proposed change gets executed: on the primary inside an open
transaction, or on a disposable replica.

**The oracle asymmetry.** The QA acceptance criterion for the diff engine
has always been: every session ends in one of two outcomes — the merge is
CORRECT, or the tool REFUSES with an actionable message. The third outcome
(reported success, wrong primary) is a hunted bug class; three rounds
killed ~17 of them, each fixed and regression-locked (Appendix A). This
doctrine is only *possible* because Path B has an oracle: the sandbox's
final state defines "correct", and fingerprints / conformance / residual
diff / history gates all verify against it. Path A has no oracle — the
validator does not know what the correct end state looks like, so it
structurally cannot implement the refuse side: it cannot detect what it
missed. "Does this SQL do what the reviewer thinks" is not a decidable
property of the text.

---

## 3. Dimension-by-dimension

(Extends rfc3 §2's C1–C7 with dimensions learned from the QA rounds.)

| # | Dimension | A: Propose-SQL | B: State-diff | Wins |
|---|---|---|---|---|
| 1 | Object of validation | program (cause) — semantics undecidable from text | state change (effect) — closed, enumerable artifact | **B** |
| 2 | Oracle / two-outcomes doctrine | no oracle; refuse side incomplete by construction | sandbox final state is the oracle; 3 QA rounds + 92-case matrix as evidence | **B** |
| 3 | Deterministic rules: scope / row count | near-parity via execute+assert+rollback — computed on the primary inside an open txn | count rows in the artifact; zero execution | **B** (A ties under caveat ①) |
| 4 | Deterministic rules: value-level / aggregate | poison value may never appear in the text (`SET first_name = credit_card`); needs transition tables on the primary = rebuilding the diff there | rules run directly on changed values (Luhn/regex/aggregate invariants) | **B** |
| 5 | Slow approval (human/LLM in loop) | txn can't be held hour-scale → two-pass → approved ≠ executed | static artifact, offline adjudication; primary untouched during review | **B** (decisive) |
| 6 | Worst case: instantaneous toxicity | poison SQL lands | poison data lands — **ceilings equal** | tie (see §4 ③) |
| 7 | Worst case: persistent attack surface | trigger/rule/function backdoors, lock DoS, rolled-back executions still cost bloat+WAL — AST must block all of it | data is inert; active objects structurally excluded (GUARD_SQL, DDL_TAGS) | **B** |
| 8 | Recoverability (undo) | no before-image; recovery = cluster PITR | swap before/after, re-run the same apply path; row-level undo | **B** |
| 9 | Conflict detection (TOCTOU) | review-time state ≠ execution-time state; predicate hits a different set, silently | before-image OCC; drift → refuse at apply | **B** |
| 10 | Covers rows arriving during isolation (C4d) | native (predicate re-evaluates) | not covered; needs re-scan / converging re-run (see T1 note) | **A** |
| 11 | Operation coverage | "supports everything" (because it verifies nothing) | explicit refusals: partitions, views/functions, type changes, `json` columns, … | **A** (see caveat ②) |
| 12 | Reviewer UX for bulk formulaic changes | one-line formula, intent readable | 100k-row diff noise, needs a summary layer | **A** |
| 13 | Artifact size | ~O(1) | O(rows changed) | **A** (evaporates on bespoke values — T1) |
| 14 | Interactive small-change latency | milliseconds, no capture round-trip | replica + freeze + capture pipeline is heavy | **A** |
| 15 | Infra dependency | stateless; decoupled from AlphaDB/sandbox machinery | replica lifecycle, freeze protocol, one-changeset-per-replica discipline | **A** |
| 16 | Eng-effort curve | small upfront + **unbounded adversarial tail** (function allowlist, DO, wCTE, SET, rewrite system; every PG release opens holes); enforcement failure surface is on the primary | large upfront + **bounded maintenance** (~30 gates, all regression-locked; closed surface) | short-term **A** / long-term **B** |
| 17 | Identity model fit (EUC rule) | agent-authored text runs as the human approver = confused deputy; approver vouches for a program they cannot evaluate | approver vouches for values; engine codegen is fixed, auditable, identical every time | **B** |
| 18 | Sequences / identity keys | nextval on primary is naturally correct | needs post-apply setval (implemented, G02/G03) | **A** (minor) |
| 19 | Audit / forensics | know what statement ran; the effect requires WAL archaeology | artifact + before-images + trajectory = complete replayable record | **B** |
| 20 | Where the agent iterates | without a sandbox, every trial-and-error step is an execute-validate proposal against the primary | arbitrary trial-and-error inside the sandbox; only the net state leaves | **B** |

---

## 4. Three honest concessions (plus one non-concession on rule granularity)

**① Rule parity is real — under one condition.** For purely deterministic,
machine-instant rules ("tables A,B,C only, < 20 rows total"), Path A
reaches near-parity: BEGIN → execute → check rowcount → ROLLBACK on
violation; the transaction is the enforcement. Two things must be said
plainly: (a) the parity *mechanism* is rebuilding the effect on the
primary — the moment rules need values or aggregates, Path A materializes
a diff via transition tables inside the lock window, i.e. it becomes
Path B computed in the worst place; (b) the parity *condition* is that the
reviewer is a machine and instant. Once the reviewer is slow, #5 reopens
the gap. **The routing variable is reviewer latency, not rule
expressiveness.**

**② Coverage without verification is not the same coverage.** Path B's
refusals are explicit and actionable; Path A's "support" for everything
contains everything it never verified.

**③ Instantaneous worst case is equal (#6) — concede it cleanly.** Under
the "LLM will fail" assumption, a malicious agent can construct a
semantically poisonous state in the sandbox (PII backfilled into
`first_name`, structure-hostile data) that is perfectly faithful and
in-scope; the deterministic gates guarantee *fidelity* and *scope*, not
benignness. Path B does not lower the toxicity ceiling. What it lowers is
**penetration probability × dwell time × recovery cost**:

- *Penetration:* the poison is concrete in the diff. `UPDATE users SET
  first_name = credit_card` is 40 characters of innocent text — the card
  numbers never appear in the SQL; AST and text scanning are blind. In the
  diff they are literal changed values; one deterministic Luhn/regex rule
  catches them without any LLM. Rules written on effects see what rules
  written on causes cannot.
- *Dwell/recovery:* before-images make committed damage mechanically
  reversible row-by-row (#8) and are forensics gold (#19). Path A's
  equivalent is cluster PITR.
- *Ceiling beyond data:* Path A's worst case additionally includes active
  objects (a planted trigger keeps acting later, with future writers'
  privileges) and operational damage (lock DoS; a rolled-back 10M-row
  update still bloats tables and spikes WAL on the primary) — unless the
  AST layer blocks that entire family, which is the unbounded-tail problem
  of #16.

**④ Rule granularity is not symmetric.** Concessions ①–③ hold at the
*ceiling*. Below the ceiling, in the region where rule-based scoped
control actually operates, A and B diverge sharply, and this is where
the engineering choice is decided in practice:

- **B admits granular, composable, statically-decidable rules over a
  closed grammar.** "Only these tables", "only these columns", "only
  this schema", "no writes to any system schema (pg_catalog / wf /
  sec)", "change size ≤ N rows", "no CREATE FUNCTION / TRIGGER / EVENT
  TRIGGER of any shape" are each one predicate over the diff artifact.
  Each is O(diff size); each composes with the others; none is a
  program-analysis problem.
- **A's rules live in SQL expression space, which is not statically
  decidable.** Whether a WHERE-clause function call has side effects,
  whether a subquery under alias/search-path/trigger resolution reads
  an out-of-scope object, whether a SECURITY DEFINER body dispatches
  dynamic SQL — Rice-theorem-shaped. Rules leak in proportion to how
  much of the expression surface the scope tries to constrain. The
  redteam trial hit this wall at Round 2: `pg_terminate_backend()` in
  a WHERE clause is AST-indistinguishable from `length()`, and no
  closed set of AST features separates them.
- **On "absolute protection against primary system functions and heavy
  queries"**: B wins by construction, not by rule quality. The agent's
  SQL never runs on the primary at all; a captured diff carries data
  changes, not function-call side effects, so control-plane calls
  (`pg_terminate_backend`, `lo_create`, `setval`, `pg_advisory_lock`)
  and heavy scans/joins execute on the sandbox and die there. A has
  no structural equivalent: every accepted statement runs on primary
  as committer.

So the routing table in §0 is not "A and B are equal at the ceiling,
pick by convenience". It is: **if you want granular, composable,
statically-decidable scope rules, you need to be checking a state diff,
not a SQL program.** Empirical evidence for this claim, including the
specific attack classes that folded a two-round-iterated A1 validator
and were inert against A2 by construction, is in
`../redteam/RED_BLUE.md`.

---

## 5. CUJ analysis

| CUJ | Change shape | Reviewer / latency | Decisive dimensions | Verdict |
|---|---|---|---|---|
| **T1 Toothbrush** (rfc1): bulk retroactive correction | thousands of rows, cross-table, formulaic **+ per-row bespoke comments** | compliance/human, hours | #5, #8 undo, #19 forensics; #10 is B's gap | **B trunk** |
| **T2 Wingman** (rfc1): interactive rebooking | handful of rows, multi-stmt txn, seconds budget | no human possible; machine rules only | #14 latency, #15 infra; EUC/PSV caps scope | **A fast path** |
| **T3 Speedy Shipping** (rfc1): speculative simulation | massive intermediate writes, mostly discarded; occasionally commit the chosen strategy | machine + decision-maker | intermediate writes force a sandbox anyway; net effect vs trajectory | **B, decisively** |
| **T4 Schema evolution / dev workflow** (devws) | DDL + backfill, PR-style | human, days | #5, #7, DDL-history hazards (QA-proven) | **B** (industry agrees: Atlas/migra) |
| **T5 Bulk import / ETL** | large-N INSERT, data originates outside the DB | sampling + stats rules | #13 evaporates (SQL carries the CSV anyway); agent-transformed data needs value checks = a diff | **lean B**; A fine for mechanical loads |
| **T6 Emergency hotfix** | 1–10 rows, operator-specified | operator is the approver, minutes | #14; replica spin-up latency unacceptable | **A fast path** |
| **T7 Agent swarm** (rfc2 names it) | many concurrent proposals | machine-dominated | where validation load lands | **B, decisively** |

### Non-obvious notes

**T1 has two reversals.** (a) If the per-order explanatory comment is
LLM-generated bespoke text, the SQL degenerates into a giant VALUES list
(rfc3 C2 predicted this) — **A's artifact-size advantage (#13) evaporates
on the flagship CUJ**. (b) B has a natural answer to #10: after merging,
re-run the task against a fresh replica; the second pass captures only
stragglers and the loop converges (cumulative capture vs the same basis is
already supported). Meanwhile A has the same window between approval and
execution — **#10 is a timing window on both paths; A merely hides it.**

**T2 is A's home ground — say so plainly.** A replica per chat turn is
absurd, and this CUJ's scope is enforced *by identity* (PSV/RLS as the
end-user) at the DB layer: the worst case is physically capped at "the
user's own booking". A's toxicity-ceiling problem (#6/#7) is rescued by
the identity model here. This is the strongest argument for the A segment
of the routing table.

**T3 looks like the most SQL-shaped CUJ and is actually B's most lopsided
win, for two stacked reasons.** First: massive intermediate writes must be
isolated and cheap — **the sandbox gets built regardless**, so A's
"decoupled from sandbox infra" advantage (#15) is zero here. Second and
deeper: what gets committed is the *net effect*, but the agent's
trajectory is hundreds of statements including dead ends. Path A would
require the agent to re-derive its trajectory into one equivalent SQL
script — an error-prone re-derivation. Path B's first-touch journal nets
it automatically (insert-then-delete cancels, trial edits collapse — the
matrix M-group netting algebra). **Trajectory ≠ net effect is the T3
restatement of "validate the program vs validate the state change."**

**T7 is an architectural difference, not a matter of degree.** Under A,
validation *is* execution on the primary (even rollbacks pay bloat, WAL
and locks), so validation load scales with swarm size on the primary —
exactly the "agent swarms overwhelming the primary" risk rfc2 calls out.
Under B, validation load scales out on replicas; the primary sees only the
serialized merge queue of already-approved short transactions.

### Summary

| | T1 | T2 | T3 | T4 | T5 | T6 | T7 |
|---|---|---|---|---|---|---|---|
| Verdict | B | **A** | B | B | lean B | **A** | B |

A's two wins (T2/T6) share one profile — small N, machine-instant rules,
identity-capped scope, latency-sensitive. Everything else lands on B for
three structural reasons: slow approval (#5), net-effect capture (T3),
validation-load placement (T7).

---

## 6. Recommended end state

Not either/or. rfc3 §4's "advanced variant" is the end state:

1. **Route by CUJ** (§0 table). A exists as a *restricted fast path*:
   single-statement or small-N, AST-bounded, machine-instant rules,
   execute+assert+rollback/commit, identity-scoped. Its validation is
   acknowledged as best-effort.
2. **B is the trunk.** SQL rides along as *review evidence* (the console
   already renders the agent's SQL trajectory in the review drawer); the
   diff is the *execution artifact*. Optional reconcile step: replay the
   SQL on a shadow and require it to reproduce the same diff.
3. **One rule engine for both paths.** Deterministic policy rules are
   written against the canonical effect representation (the delta format).
   Path B feeds it the sandbox artifact; Path A feeds it in-txn transition
   tables. The diff format is the lingua franca either way — which is
   itself evidence for which representation is fundamental.

External cross-check: for schema, the industry has already voted —
migra / Atlas / pgroll are catalog-diff → generated DDL, and the most
successful propose-SQL product (PlanetScale deploy requests) is
schema-only. **There is no propose-SQL product for data DML**, precisely
because DML text is not reconcilable. Scoop's engine treats schema the way
Atlas does (catalog diff → generated DDL) and extends the same idea to
data (journal diff → generated DML). Path A treats both as opaque text.

One prior decision is also binding here: client-triggered capture was
already rejected in the capture-trust analysis (artifact-output interface
= exfiltration/tamper surface; DEMO_DESIGN §1.5). Propose-SQL is the same
rejected trust model — client-supplied artifact — returned through another
door, except the artifact is now a *program* that runs on the primary
under a human's identity.

---

## Appendix C — adversarial evidence, red-team trial

Separate from the 92-case regression matrix (Appendix A), a red/blue
adversarial trial was run against both A and B under matched scenarios
(fixed table set → +column ops → schema-level scope). See
`../redteam/RED_BLUE.md` for the full narrative. Highlights:

- **A1 (propose-SQL, pglast validator)** folded on Scenario 1 after
  three rounds. v0.1 (5 rules, top-level UpdateStmt check) lost 4/4
  attacks to writable-CTE and SET-RHS-subquery class. v0.2 (9 rules,
  full-tree Visitor) closed that class but lost 3 attacks to the
  volatile-function-in-WHERE class (`pg_terminate_backend`,
  `lo_create`). Proposed v0.3 (catalog `provolatile` check) did not
  land after operator+judge determined the class is structurally
  open-ended (~2500 built-in functions, extensions, CREATE FUNCTION,
  SECURITY DEFINER bodies).
- **A2 (state-diff)** on Scenario 1: 19 self-play attacks, 0 BREAK
  after adding a ~40-LOC `enforce_scope` gate. On Scenario 2
  (+column ops): 25 self-play attacks, 0 BREAK, with fixes for
  `SYSTEM_SCHEMAS` always-out set, `ok`-flag honesty, `wf_applied`
  moved out of `public`, sandbox fork excluding `sec`+`wf`. Scenario 3
  is designed but the engine refactor is deferred; containment
  argument is that the diff artifact remains a closed grammar
  regardless of scope description style.
- **Direct feed to §4 ④**: A's failures on S1 were the volatile-fn
  class; B was structurally immune to that class in S1 and remained
  immune in S2, because the diff artifact does not carry function-
  call side effects.

## Appendix A — adversarial evidence (what "validate the state change" survived)

Three QA rounds where a sub-agent operated the sandbox DB directly and
tried to produce outcome #3 (reported success, wrong primary). ~17 kills,
each fixed + regression-locked; matrix now 92/92 (`prodpoc/test_matrix.py`
groups R, G, H). By class:

| Class | Representative kills | Locked by |
|---|---|---|
| Lossy projection used for both transport and verification | psycopg2 float decode of `numeric`; jsonb scalar null ↔ SQL NULL; float `-0.0`; array lower bounds; `json` column normalization | wf_json (Decimal-exact); `value_gate()` at install + capture (H02, H08, H09, H11, R01) |
| Rendering is session-state-dependent | `extra_float_digits=-3` truncation; DateStyle/lc_monetary | `STABLE_GUCS` pinned on capture, apply, and both verifiers (H10) |
| History-invisible DDL | net-zero ALTER TYPE; same-type USING rewrite; drop+re-add column; RENAME cycles; drop+recreate table | per-step catalog frames + relfilenode + same-name drop/re-add refusal (R03–R07, H03) |
| Catalog-window escape | `SET SCHEMA` / rename to `scratch*` captured as drop_table → would DELETE the primary's table with both verifiers agreeing | basis oid check: only a dead oid is a drop (H01) |
| Effects outside the column model | CREATE VIEW/FUNCTION/COMMENT diff to nothing; DISABLE TRIGGER; RLS; UNLOGGED | DDL_TAGS + GUARD_SQL (H12) |
| Journal silencing | `session_replication_role='replica'` | ENABLE ALWAYS triggers (H13) |
| Session-wedging crashes | no-PK new table → IndexError with txn open | refusal + structured-error catch-all (H06) |

Every one of these was findable and fixable **because the artifact is
closed and has an oracle**. Each row doubles as a question for Path A's
validator: *how would you have caught this in SQL text?*

## Appendix B — capture code path (Path B mechanics)

`proto/prodpoc/` — `wf_capture.py` (capture), `wf_sqlgen.py` (apply +
verifiers), `wf_sidecar.py` (replica-side HTTP daemon), `wf_json.py`
(Decimal-exact serialization).

1. **Install** (`/v1/session/start` → `install()`): journal schema; three
   statement-level AFTER triggers per scoped table (transition tables,
   first-touch `ON CONFLICT DO NOTHING`) + TRUNCATE blocker, all
   ENABLE ALWAYS; one `ddl_command_end` event trigger appending a full
   catalog frame per DDL; basis snapshots into `wf.meta` (scope,
   fingerprints, catalog, relfilenodes, table oids, guard snapshot);
   `value_gate()` screens basis data.
2. **Agent works** — zero interception. DML leaves first-touch
   before-images; DDL leaves catalog frames. No SQL text recorded.
3. **Capture** (server-triggered, replica frozen): one REPEATABLE READ
   txn, GUCs pinned; gates first (net catalog whitelist; per-step history
   + tag whitelist; drop/recreate; relfilenode; oid-alive; guard diff;
   rename hazard; partitions; no-PK; value gate), then deltas
   (journal ⟕ live table → I/U/D with `changed` computed on canonical
   text; new tables full-scan; ADD-COLUMN backfill value reconstructed
   from two independent evidence sources). Output: static
   `{basis_fp, final_fp, schema_delta, data_delta, stats}` — the material
   basis of offline adjudication (#5).
