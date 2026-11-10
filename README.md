# Final Project: Portfolio Monitoring & Rebalancing Agent Workflow

- **Student ID**: 25210660108
- **Course**: AI Coding in Finance, Fall 2026
- **Repository**: https://github.com/Tracyqq1b/25210660108-final-project

## Summary

A long-running agent workflow for a stock/ETF multi-asset portfolio
(AAPL, MSFT, NVDA, QQQ, IEF, COST; USD). Every day after the close it runs
ingestion (S1) → risk monitoring (S2) → signal generation (S3) →
independent review (S4) → human approval (S5) → sandbox execution and
reconciliation (S6, on a T+1 model: approval places a pending order that
fills at the next trading day's price, with the price difference
disclosed). At each month end, S7 evaluates the monitoring rules against a
fixed baseline and produces a parameter-change proposal that requires
human approval. All state persists in SQLite; a crash at any stage resumes
exactly from checkpoints, and completed stages never re-execute. All
arithmetic is deterministic code; LLM agents carry only the narrative and
judgment layer (see `prompts/`).

## File guide

| Path | Content |
| --- | --- |
| `docs/01-task-spec.md` | Part 1: task specification (stages, triggers, data sources, cutoffs, acceptance criteria) |
| `docs/02-design-note.md` | Part 2: design note (roles & handoffs, long-running operation, verification & improvement) |
| `docs/03-workflow-diagram.md` | Annotated workflow diagram (Mermaid) + annotations |
| `docs/evidence/` | Verification & recovery evidence: test output, full replay log, crash-recovery log, rules lifecycle log, async approval flow log, reconciliation log |
| `prompts/` | Role prompts for the three agents (S3 signal, S4 review, S7 rules evaluation) |
| `src/config.yaml` | Portfolio, rule thresholds, execution parameters, runtime limits and permission declarations |
| `src/pma/` | Implementation: `datafeed.py` (data layer, as-of cutoff), `state.py` (SQLite state/checkpoints/rule versions/pending orders), `stages.py` (S1–S6), `review.py` (S4 independent review), `controller.py` (orchestration/retries/resume/approval gate), `rules_eval.py` (S7), `render.py` (daily/monthly report rendering), `notify.py` (notification channels), `cli.py` (CLI) |
| `data/cache/` | Real market-data cache (daily bars from the East Money kline API, 2024-09 to 2025-10) |
| `examples/inputs/` | Example inputs (portfolio definition, broker positions snapshot) |
| `examples/outputs/` | Example outputs: the full 2025-09-02 chain (proposal+review+approval+pending order), 2025-09-03 fills (T+1), month-end S7 artifacts, and rendered reports under `reports/` |
| `tests/` | 24 tests: crash recovery, no future-information leakage, approval gate & review independence, T+1 execution, async approval, reconciliation, report rendering |

## Setup and run

Environment: Python 3.9+.

```sh
pip3 install -r requirements.txt   # pandas, pyyaml, requests (data fetch)
```

`data/cache/` is committed with the repo, so every command below
**reproduces offline**; to refresh market data:
`python3 src/pma/cli.py refresh --start 2024-09-01 --end 2025-10-31`
(this command needs the network).

```sh
# 1. Initialize the state store (seeds the example portfolio and rules v1)
python3 src/pma/cli.py init

# 2. Replay a stretch of history (S1–S6 daily, S7 at month ends;
#    --auto-approve is the demo mode and is marked in the audit trail)
python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-10-31 --auto-approve --with-rules-eval

# 3. Single day in approval mode: a breached proposal pauses S5, writes
#    runs/approvals/<date>.md and pushes a notification (desktop/webhook/
#    email, see the notifications section in config.yaml); deciding resumes
python3 src/pma/cli.py run-day 2025-09-02
python3 src/pma/cli.py approve --day 2025-09-02          # or --reject

# 4. Crash recovery demo: inject a crash after S3 (exit code 75), then
#    re-run the same command to resume from S4
python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-09-12 --auto-approve \
    --crash-after S3 --crash-on 2025-09-10 --db runs/crash-demo.db
python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-09-12 --auto-approve --db runs/crash-demo.db

# 5. Rules lifecycle: apply the S7 change proposal / roll back
python3 src/pma/cli.py apply-rules --yes
python3 src/pma/cli.py rollback-rules --to 1 --yes

# 6. Inspect state / export a day's artifacts / render reports
python3 src/pma/cli.py status
python3 src/pma/cli.py export --day 2025-09-02 --out /tmp/pma-export
python3 src/pma/cli.py report --day 2025-09-02        # daily report (auto-written to runs/reports/ during replay)
python3 src/pma/cli.py report --month 2025-10        # monthly rules report

# 7. Positions reconciliation: drop a broker snapshot into the inbox and S1
#    reconciles it; on divergence, book the outside-workflow change as an
#    append-only adjustment, then re-run the day
cp examples/inputs/positions_2025-09-02.csv data/inbox/
python3 src/pma/cli.py import-positions --file data/inbox/2025-09-02_positions.csv \
    --day 2025-09-02 --yes

# 8. Tests (24: recovery, leakage, approval gate, T+1, async approval,
#    reconciliation, report rendering)
python3 -m unittest discover -s tests -v
```

## Where the verification evidence lives

- Test output: `docs/evidence/tests.log` (24/24 pass)
- Full 44-trading-day replay: `docs/evidence/replay.log`
- Crash → resume: `docs/evidence/recovery.log` (S1–S3 skipped by
  checkpoints, attempts stay 1)
- Async approval: `docs/evidence/approval-flow.log` (S5 pauses → request
  document + notification → decision → automatic resume)
- Positions reconciliation escalation: `docs/evidence/reconciliation.log`
  (snapshot divergence → S1 stops → human booking → resume)
- Rules v1 → v2 → (rollback) v3: `docs/evidence/rules-lifecycle.log`
- Full rebalance chain: `examples/outputs/2025-09-02/` (approval day) +
  `examples/outputs/2025-09-03/` (next-day fills with price differences)
- Rendered report samples: `examples/outputs/reports/`

## Own contribution

The topic and task framework (stage breakdown, approval flow, rules
evaluation idea) are my own design; the final architecture, all code,
tests, and documentation were produced in pair-programming with an AI
coding assistant (Kimi Code), with me responsible for requirements
trade-offs, design decisions (deterministic computation with the LLM
confined to the narrative layer; the approval hash chain; T+1 execution;
window-start reconstruction against leakage) and acceptance of results.
See the Git history for the incremental development process (progressive
commits, not a single import).
