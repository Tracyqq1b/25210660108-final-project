# Part 1: Task Specification

> Final Project — Design a Long-Running Agent Workflow for a Finance Task
> Topic: Portfolio Monitoring & Rebalancing Agent

## 1.1 Task overview

Build a long-running agent workflow for a stock/ETF multi-asset portfolio
that closes the full loop every day: end-of-day data ingestion, risk
monitoring, rebalancing signal generation, independent review, human
approval, sandbox execution with reconciliation — plus a monthly rules
evaluation on top of the accumulated history.

**Intended user**: an individual investor / an assistant to a small asset
manager's portfolio manager. The user does not want fully automated
trading; they want a system that absorbs the mechanical daily monitoring
and arithmetic, and only surfaces a reviewed proposal when action is
needed, waiting for their approval.

## 1.2 Current process and its problems

The user's current manual process:

1. After each close, download quotes by hand and update the positions sheet
2. Compute weights, drawdown, and sector concentration in a spreadsheet
3. On a breach, compute order quantities by hand, place orders, record them
4. Never revisit whether the monitoring rules themselves are sensible

**Problems**: time-consuming, easy to miss breaches, no audit trail, rule
tuning driven by gut feel. The common root cause: the process depends on
**continuously accumulating state** (positions, fills, historical rule
versions) and repeats every single day.

## 1.3 Stages and dependencies

| Stage | Content | Depends on | Trigger | Output |
| --- | --- | --- | --- | --- |
| S1 Ingestion | Pull the EOD market snapshot (with cache-coverage record); if a broker positions snapshot is in the inbox, reconcile it first | — | Scheduled after close / event-driven | Standardized market snapshot + reconciliation result |
| S2 Risk monitoring | Weights, drift, drawdown, sector concentration; record equity | S1 | With S1 | Risk report (with breach flags) |
| S3 Signal generation | Turn breaches into a rebalance proposal (side, quantity, rationale) | S2 | On breach | Rebalance proposal |
| S4 Independent review | Reviewer recomputes everything from raw data and checks the proposal | S3 | With S3 | Review verdict (pass/fail) |
| S5 Human approval | Approval request written to disk and pushed (desktop/webhook/email); operator decides with the `approve` command; the run resumes asynchronously | S4 pass | Awaits human (async, non-blocking) | Approval record (with proposal hash) |
| S6 Execution & reconciliation | Day-T approval → pending order; on T+1 fill at that day's price, reconcile, update positions/cash atomically | S5 | Day after approval | Pending orders, fill log, reconciliation report, new positions |
| S7 Rules evaluation | Evaluate the month's rule performance, replay candidates against a fixed baseline, propose parameter changes | Accumulated S2–S6 history | Last trading day of month | Evaluation report + change proposal (with replay evidence) |

**Dependency notes**: S2→S6 is a daily pipeline where each day depends on
the previous day's position state (positions and cash change only through
S6 fills). S7 depends on the accumulated equity curve, breach history, and
fill history — a classic cross-period dependency.

## 1.4 Triggers

- **Scheduled**: S1 starts T minutes after the close on trading days
  (production reference: 16:30 US/Eastern; the demo uses `replay` to drive
  the trading calendar)
- **Event-driven**: an announcement/corporate action on a holding inserts
  an extra S1–S2 pass (`run-day` is the manual equivalent)
- **Periodic**: S7 starts on the last trading day of each month
  (`replay --with-rules-eval` fires it at month boundaries)
- **Manual**: the user can launch a full run or re-run a single day at any
  time; checkpoints make re-runs skip completed stages automatically

## 1.5 Inputs and permitted data sources

- **Market data**: the East Money push2his kline API (a publicly queryable
  market-data database; forward-adjusted daily bars), pulled once into a
  local CSV cache; replay and tests read the cache only
- **Positions data**: broker positions snapshot reconciliation — the user
  drops a broker export CSV (`symbol,qty` plus an optional `CASH` row)
  into the inbox `data/inbox/<date>_positions.csv`; S1 reconciles it
  against recorded state before proceeding. A mismatch means something
  happened outside the workflow (manual trade, dividend, split): the
  pipeline stops and escalates, and a human books the external change as
  append-only `adjustments` via `import-positions`. Production
  replacement: a read-only broker positions API
- **Source of truth**: the workflow's own SQLite store is always the
  single source of truth; external snapshots are reconciliation inputs and
  never silently overwrite state
- **Announcements / corporate actions**: exchange announcements, public
  information (not wired into the reference implementation; the event
  trigger entry point is reserved, and dividends/splits are booked via
  `adjustments`)
- **Explicitly excluded**: social-media sentiment, unauthorized data from
  paid terminals

## 1.6 Information cutoffs

- Decisions may only use data available **before** the decision point. The
  daily batch is cut off at that day's close.
- **Decision and execution are strictly separated in time**: proposals are
  generated and approved after the day-T close using day-T data, and fill
  at day-T+1 prices — a decision made with close data can never fill at
  that same close. The pending-order model eliminates this implicit
  look-ahead by construction. The reconciliation discloses, per fill, the
  approval-day reference price versus the actual fill price.
- Implementation: the data layer (`datafeed.load` / `price_on`) is the
  single entry point for all market reads; it requires an explicit `as_of`
  and never returns later rows; a non-trading day raises instead of
  silently falling back to stale prices. `tests/test_no_leakage.py`
  spot-checks this across multiple dates.
- S7 baseline replays reconstruct history as knowable at the time: the
  window-start portfolio is rebuilt exactly by **reversing the recorded
  fills and adjustments inside the window**, each replay day reads prices
  cut off at that day, and simulated fills use the same T+1 pending-order
  model as the live path — no future data.

## 1.7 Work products

1. **Daily end-of-day risk report** (Markdown, auto-rendered to
   `runs/reports/<date>.md`): portfolio overview (value, weights,
   drawdown), breach alerts, action outcome (including the
   proposal/review/approval chain), and the audit-chain hashes; quiet days
   get a one-line "no action needed", breach days expand to full detail
2. Rebalance proposal + review verdict + approval record (the three-part
   chain as JSON, linked by content hashes, auditable)
3. Fill and reconciliation log (S6 output, including post-fill positions)
4. Versioned history of positions and rule parameters (SQLite; rule
   versions are append-only and rollback-capable)
5. **Monthly rules-evaluation report** (Markdown,
   `runs/reports/<month>_rules.md`): fixed-baseline replay comparison
   table + change conclusion

## 1.8 Acceptance criteria (quantified)

- The daily pipeline finishes within **2** hours of its trigger
  (`limits.daily_deadline_minutes: 120`); a failing stage retries at most
  **3** times with exponential backoff, then errors out and escalates
- Zero missed risk breaches: S4 independently recomputes all weights and
  breaches from raw data; any disagreement with the proposal fails the
  review and blocks S5/S6
- Every order maps to an approval record, with side and quantity matching
  the approved proposal one-to-one (`tests/test_review_gate.py`; the
  approval stores the proposal's content hash — a tampered proposal no
  longer hashes to the approved value and is refused). Approval-day
  reference price versus next-day fill price is disclosed per fill in the
  reconciliation
- After an interrupted run resumes, completed stages are never re-executed
  (`tests/test_recovery.py` and `docs/evidence/recovery.log`: after a
  crash, resumed stages keep `attempts = 1`)
- A rule-change proposal may enter approval only with fixed-baseline
  replay results attached (baseline = buy-and-hold over the same window
  plus a replay of the current rules); approval writes a new version,
  rollback is one command

## 1.9 Why a persistent workflow, not a one-shot script

Three reasons, matching the project's hard requirements:

1. **State accumulation**: positions, approvals, and rule versions evolve
   across days and months; a one-shot script has no concept of "yesterday"
2. **Recurring triggers**: the task is driven by the calendar and by
   events; something must start it unattended, repeatedly
3. **Recovery**: one run spans multiple side-effecting stages (orders,
   position writes); after a crash mid-run, execution must resume
   precisely from durable checkpoints rather than start over — that is
   exactly why checkpoint and idempotency design exists
