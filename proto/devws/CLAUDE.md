# Developer workspace — CymbalAir database changes via Wildfire

You are a developer agent working on the CymbalAir production database
(AlloyDB + AlphaDB). You NEVER get direct write access to the primary.
All changes go through the **Wildfire** protocol using the `./wf` CLI in
this directory (it wraps the control-plane API; a future MCP tool has the
same surface).

## Database schema (cymbalair)

- `flights(id, flight_no, origin, dest, departs_at, status)`
- `seats(id, flight_id→flights, seat_no, cabin, status available|booked, booking_id)`
- `bookings(id TEXT pk — client-generated like 'BK-…', flight_id→flights,
  passenger, seat_no, price numeric, status confirmed|cancelled, created_at)`
- plus whatever prior merges added — always explore first.

## Workflow (follow strictly, in order)

1. Get a replica (your isolated, read-only copy of the database). A replica
   is infrastructure: it is provisioned for an identity and a role, and it
   outlives any single task. Two ways to get one:
   - If the operator gave you a replica id (looks like `rd-<name>`):
     `./wf attach <replica_id>`
   - Otherwise self-provision:
     `./wf checkout --actor <you> --role developer [--name <replica-name>]`
2. Explore: `./wf sql "SELECT ..."` (as often as you like).
3. `./wf detach` — freezes your basis and enables local writes.
4. Make changes with `./wf sql "<INSERT/UPDATE/DELETE/DDL>"` — they only
   touch YOUR sandbox. Verify your own work with SELECTs before proposing.
5. `./wf propose --task "<what this change is for>" --note "<how you did it>"`
   — the task is per-changeset, not per-replica: state it here, in the
   user's terms, because the reviewer reads it as the intent to check the
   diff against. Propose captures the net diff + your full command
   trajectory. Row-level DML may auto-merge after LLM review; anything
   with DDL always waits for a human reviewer.
6. If status is pending_human: `./wf wait` (blocks; exit 0 = merged).
   Note the semantics: a human reviewer only APPROVES — approval does not
   execute anything. When `wf wait` sees the approval it submits the merge
   under YOUR identity, and the primary database checks YOUR privileges
   (grants, ownership, RLS) as the writes run. Your identity is `devbot`
   — always use it as `--actor` so your merge requests carry real
   database permissions.
7. After a merge: `./wf resync` then verify with `./wf sql` that the
   primary now shows your change (your reader re-clones from primary).
8. If propose/merge fails (`data_conflict`, `schema_drift`, `rejected`):
   `./wf resync`, re-check current state, redo the work, propose again.

## Rules

- Writes before `detach` will be refused (reader is read-only while attached).
- Be honest in your summaries: a change is only DONE when `wf wait` says
  `merged`. `pending_human` means NOT applied yet.
- Keep changesets minimal and single-purpose; one task = one propose. The
  same replica can serve several tasks in a row — each gets its own
  `--task` at propose time.
- New rows need client-generated text ids (e.g. 'BK-<something unique>').
- `./wf state` shows cluster stats and the review queue at any time.
