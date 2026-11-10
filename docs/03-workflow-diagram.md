# Part 3: Annotated Workflow Diagram

```mermaid
flowchart TD
    subgraph TRIG["Trigger layer (controller)"]
        T1["Scheduled: after each close<br/>cron → run-day"]
        T2["Event: corporate action / announcement<br/>→ run-day (inserted)"]
        T3["Periodic: last trading day of month<br/>→ S7"]
        T4["Manual: replay / run-day / re-run"]
    end

    subgraph DAY["Daily pipeline (linear deps, checkpoint per stage)"]
        S1["S1 Ingestion<br/>deterministic code<br/>as-of cutoff, cache read-only<br/>+ positions snapshot reconciliation<br/>(divergence stops the day)"]
        S2["S2 Risk monitoring<br/>deterministic code<br/>weights / drawdown / concentration<br/>+ records equity"]
        S3{"S3 Signal generation<br/>code for math + LLM narrative<br/>skips names with in-flight orders"}
        S4["S4 Independent review<br/>deterministic code, recomputes<br/>from raw data; reuses none of<br/>S3's numbers"]
        S5["S5 Human approval ⛔<br/>async gate: request written to disk<br/>+ pushed; approve command decides;<br/>checkpoint resume"]
        S6["S6 Sandbox execution & recon<br/>deterministic code; T+1 model:<br/>fill in-flight orders first, then turn<br/>today's approval into pending orders"]
    end

    subgraph MONTH["Cross-period evaluation"]
        S7["S7 Rules evaluation<br/>fixed-baseline replay + candidate grid"]
        AP2["Parameter-change approval ⛔<br/>apply-rules"]
        RB["Rule versions (append-only)<br/>rollback-rules"]
    end

    subgraph STATE[("SQLite persistent state runs/state.db<br/>positions/cash · trades · approvals · pending orders<br/>checkpoints stage_runs · artifacts (content-addressed)<br/>rule versions · equity curve · adjustments"))]
    end

    T1 --> S1
    T2 --> S1
    T4 --> S1
    S1 --> S2 --> S3
    S3 -- "no breach: HOLD" --> S5
    S3 -- "breach → proposal" --> S4
    S4 -- "fail: day stops,<br/>bounded revision loop" --> S3
    S4 -- "pass" --> S5
    S5 -- "rejected" --> END1["day archived, nothing executed"]
    S5 -- "approved (hash match)" --> S6
    S6 --> END2["pending orders placed; fills +<br/>reconciliation land next trading day"]
    T3 --> S7
    S7 --> AP2 --> RB
    RB -. "new version effective next day" .-> S2

    S1 <--> STATE
    S2 <--> STATE
    S3 <--> STATE
    S4 <--> STATE
    S5 <--> STATE
    S6 <--> STATE
    S7 <--> STATE
    RB <--> STATE
```

## Annotations

**Main line (daily)**: S1→S6 is strictly linear. Before each stage the
controller consults the `stage_runs` checkpoint table: a stage already
`done` for that day is skipped wholesale — this is the mechanism by which
recovery never repeats completed work (evidence:
`docs/evidence/recovery.log`).

**Two decision points**:

1. **S3 breach check**: a quiet day yields a HOLD proposal; S5
   auto-archives it ("no action required") and S6 records a no-op. Only a
   breach reaches review.
2. **S4 review**: a fail stops the day at S4 and loops back to S3 — a
   bounded revision loop (bounds: ≤ 3 retries, 2-hour daily deadline,
   escalate to a human beyond that).

**Two human gates (⛔)**: S5 before any order, and `apply-rules` before
any parameter change. Both leave append-only approval records in SQLite;
S6 enforces that the approval's proposal hash matches the current
proposal before creating pending orders.

**State flow**: stages exchange data only through the store, never by
calling each other. The only write paths into positions/cash are S6's
transactional fills and human-confirmed `import-positions` adjustments;
the only write path into rule parameters is `apply-rules` /
`rollback-rules` (append-only version rows).

**Cross-period loop**: each month end, S7 evaluates the rules from the
accumulated equity curve, breach history, and fills; an approved proposal
becomes a new version (v2) effective for the next day's S2.
`docs/evidence/rules-lifecycle.log` shows the fully audited cycle
v1 → v2 (approved) → v3 (rollback to v1's parameters).

**Leakage boundary**: S1 is the only stage that touches external data,
through the data layer's enforced `as_of` cutoff; S7 replays are bounded
day by day the same way, and fills happen on T+1 so decisions never trade
at decision-time prices (see `docs/02-design-note.md` §2.3).
