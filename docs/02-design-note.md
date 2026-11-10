# Part 2: Architecture Design Note

Companion diagram: `docs/03-workflow-diagram.md`. Implementation: `src/pma/`.

## 2.1 Roles and handoffs

The workflow has two kinds of components: the **controller** owns
execution control, and the **stages** own domain logic. All arithmetic
inside stages is deterministic code; LLM agents carry only judgment and
narrative work, and their output never enters the numeric pipeline.

| Role | Responsibility | Tools / permissions | Input → Output |
| --- | --- | --- | --- |
| Controller (code, `controller.py`) | Dependency ordering, scheduling, retries, checkpoint resume, crash injection, approval gate | SQLite read/write; no network | Trading day → invokes S1–S6 in order |
| S1 Ingestion (code, `stages.s1_ingest`) | Pull the EOD snapshot via the data layer; if an inbox positions snapshot exists, reconcile first — divergence stops the day and escalates | Data layer read-only (network only in the `refresh` command; offline at run time); inbox read-only | Date → market snapshot + reconciliation result |
| S2 Risk monitoring (code, `stages.s2_monitor`) | Weights, drawdown, concentration, breach detection; records equity | State store read + equity write | Snapshot → risk report |
| S3 Signal agent (code for math + LLM for narrative; prompt `prompts/signal-agent.md`) | Breaches → rebalance proposal (side/qty/rationale) | No tools; input is the S2 report | Risk report → rebalance proposal |
| S4 Independent review (code, `review.py`) | **Reuses none of S3's numbers**: recomputes from raw positions and raw prices, checks item by item | State store read-only, data layer read-only | Proposal → pass/fail + check list |
| S5 Human approval (human; `stages.s5_approval` is the async gate) | Approval request written to `runs/approvals/` and pushed (desktop/webhook/email); the `approve` command records the decision; HOLD auto-archives; `--auto-approve` exists for demos/tests and is always marked in the audit note | Notification channels + CLI | Proposal + review → approval record (with proposal hash) |
| S6 Execution & reconciliation (code, `stages.s6_execute`) | T+1 model: day-T approval → pending order; on T+1 fill at that day's price (± slippage), pre-flight cash/inventory checks, atomic booking, price-difference disclosure | State store transactional writes | Approval → pending orders → next-day fill log + reconciliation |
| S7 Rules evaluation agent (code replay + LLM summary; `rules_eval.py`, prompt `prompts/rules-agent.md`) | Monthly rules retrospective; candidate parameters replayed against a fixed baseline; change proposals | State store read-only, data layer read-only | Monthly history → evaluation report + change proposal |
| Report renderer (code, `render.py`) | Renders artifact chains into human-readable daily/monthly Markdown reports; does no arithmetic — every figure is copied from artifacts, audit-chain hashes in the footer | State store read-only | Artifacts → daily report / monthly report |

**Why the work is divided this way**

- **Code computes, agents judge**: weights, order quantities,
  reconciliation, and replays are deterministic computations — an LLM
  would only add irreproducible numeric risk. The LLM's value is in S3's
  rationale writing, S4's opinion drafting, and S7's retrospective
  narrative — explanations of structured results that never feed back into
  the numbers. The human-readable reports are mechanically rendered from
  artifacts by `render.py`; the skeleton and all figures are guaranteed by
  code, and LLM narrative only fills explanatory prose.
- **S4 is independent of S3**: the reviewer re-reads positions from the
  store and prices from the cache, recomputes weights, breaches, and
  simulated post-trade weights, then compares against what the proposal
  claims. It is not a second read of the proposal; it is an independent
  recomputation (`test_review_fails_tampered_prices` proves tampering
  fails review).
- **Agent decisions vs controller execution control**: "rebalance or not,
  change parameters or not" are agent/human decisions; "when to run, which
  stage, how many retries, where to resume after a crash" belong entirely
  to the controller, and stages are unaware of them.

**Handoffs and the audit chain**: every stage produces a content-addressed
(SHA-256) artifact; S3 cites S2's breach list, the S5 approval records the
S3 proposal's hash, and S6 fills record the approval ID. Tampering with
any link breaks a downstream hash check.

## 2.2 Long-running operation

**Persistent state and checkpoints**: a single SQLite store
(`runs/state.db`) holds: positions/cash (current state), fills and
approvals (append-only), **pending orders (`pending_orders`: approved on
day T, filled on T+1 — cross-day state)**, external `adjustments`
(append-only: manual trades, dividends, splits booked via reconciliation),
the `stage_runs` checkpoint table (`running/done/failed/waiting` plus
attempts, keyed by `run_date × stage`), the artifact table, the rule
version table, and the equity curve. The store is the single source of
truth: positions change through exactly two audited paths — approved S6
fills, or human-confirmed `import-positions` adjustments.

**Idempotent resume**: before running any stage the controller checks its
checkpoint: `done` stages are skipped untouched (not even `begin_stage` is
called, so `attempts` does not grow — verifiable evidence of "not
re-executed"). Restarting the same command after an interruption resumes
exactly at the first incomplete stage. `docs/evidence/recovery.log` shows
a crash after S3 and a restart that skips S1–S3.

**Positions reconciliation and escalation**: if the inbox snapshot
disagrees with recorded state, S1 raises `PositionsDivergedError`; after
retries exhaust, the day stops at S1 (`docs/evidence/reconciliation.log`
shows the full loop: divergence → stop → human booking → checkpoint
resume). Operations outside the workflow can never be silently lost — and
S7's window-start reconstruction (`reconstruct_start_state`) reverses
both fills and adjustments, so the historical state at any day is exactly
recoverable.

**T+1 execution and in-flight orders**: approval creates a pending order
rather than an immediate fill; the next day's S6 first works through
in-flight orders at that day's prices (sells first for self-funding;
insufficient cash/inventory stops the day and escalates), then converts
that day's approved proposal into new pending orders.
`fill_pending_order` is a single transaction (trade row + position/cash +
order status flip), so a crash or retry never fills twice. Signal and
review both account for in-flight orders: S3 does not double-order a
symbol with a pending order (noted as deferred in the proposal), and S4
verifies each in-flight order's direction actually repairs its breach.

**Dependencies and scheduling**: S1–S6 are strictly linear; S7 depends on
a month of accumulated history and fires at month boundaries
(`replay --with-rules-eval` runs it on each month's last trading day,
checkpointed like any stage). In production, `run-day` hangs off an
after-close scheduler (e.g. cron 16:30 ET), and an event trigger is
equivalent to running `run-day` manually.

**Retries and escalation**: stage failure → `fail_stage` records it →
exponential backoff (2^n s, capped at 8s), at most 3 retries; then a
`PipelineError` ends the day, preserving the scene for a human
(escalation rule: exhausting retries escalates; errors are never silently
swallowed).

**Limits and permissions** (declared in `src/config.yaml` under `limits`,
enforced by the controller/execution layer):

- Stage timeout 300 s; daily deadline 2 hours
- Per-order notional cap $20,000 (enforced in S6 pre-flight)
- Network: only the data-refresh command may use the network; all run-time
  stages are offline
- Trading: sandbox only; filesystem: project directory only

**Time and cost**: a daily run is six lightweight local-computation
stages, far below the limits; the genuinely expensive operations (network
fetches, human approval) are isolated into explicit commands.

## 2.3 Verification and improvement

**Checks independent of the producer**: S4 review (see 2.1). In addition,
`tests/` freezes three property families: recovery
(`test_recovery.py`), no future information (`test_no_leakage.py`), and
the approval gate plus review independence (`test_review_gate.py`), async
approval (`test_async_approval.py`), reconciliation
(`test_reconciliation.py`), and report rendering (`test_report.py`).

**Human approval before consequential actions**: two human gates — S5
(before any order) and `apply-rules` (before any rule-parameter change).
Both write append-only approval records. S5 is an **asynchronous gate**:
once a proposal passes review, the workflow writes an approval request
(`runs/approvals/<date>.md`, with triggering breaches, proposed trades,
and the review verdict) and pushes it through the configured channels
(desktop notification / webhook / email; the SMTP password is read only
from an environment variable), then pauses the day in a clean `waiting`
state — holding no process, counting no failure, consuming no retries.
The operator decides (`approve --day <date> [--reject]`) and the day
resumes from checkpoints automatically. Evidence:
`docs/evidence/approval-flow.log`.

**Bounded revision loop**: S3→S4 form the intra-day revision loop: a
failed S4 stops the day at S4 without reaching approval or execution;
after the proposal is fixed the day re-runs and the S4 checkpoint is
recomputed, bounded by retry ≤ 3 and the 2-hour daily deadline, with
escalation to a human at the bound.

**Change testing, acceptance, versioning, rollback** (the S7 chain):

1. **Fixed baseline**: at each month end, the window-start state is
   reconstructed exactly (reversing in-window fills and adjustments), and
   three rule sets replay over the same window: buy-and-hold baseline,
   current rules, and candidates (drift-band grid 0.03/0.05/0.08).
2. **Accept/reject**: a candidate becomes a proposal only if it beats the
   baseline (end value no lower than buy-and-hold) and has strictly fewer
   breach days than the current rules; the proposal enters human approval
   with evidence attached, and rejection leaves no trace in the rules
   table.
3. **Versioning**: approval appends a new `rule_versions` row (superseding
   the old one; parameters and rationale kept forever).
4. **Rollback**: `rollback-rules --to N` issues a new active version whose
   parameters equal the target's — rollback is itself an audited change;
   history is never rewritten.

**Future-information leakage control**: the data layer's single entry
point enforces `as_of`; S7 replays reconstruct the window start by
reversing recorded events rather than querying "what we knew then", with
each replay day cut off at itself; non-trading-day price lookups raise.
Tests spot-check multiple dates.

## 2.4 Reference implementation vs production replacement points

| Reference implementation (this repo) | Production replacement |
| --- | --- |
| East Money kline API + local cache | Broker/vendor real-time feed |
| Positions CSV inbox + human reconciliation | Read-only broker positions API + automatic reconciliation |
| Sandbox fills (close ± slippage) | Broker paper/live trading API |
| `replay` driven by the trading calendar | cron/scheduler + event listeners |
| LLM narrative layer delivered as prompt files (not wired to a runtime) | Any LLM harness instantiating the roles per `prompts/` |
