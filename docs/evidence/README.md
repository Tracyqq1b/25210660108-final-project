# Verification & Recovery Evidence

Raw, unedited outputs of the workflow's verification runs.

| File | What it proves |
| --- | --- |
| `tests.log` | All 24 automated tests pass: checkpoint resume never repeats completed stages, a crashed run reaches the same state as a clean run, the as-of cutoff blocks future rows, no execution without approval, hash-mismatched approvals are refused, tampered proposals fail independent review, T+1 fills land the next day, async approval pauses/resumes cleanly, reconciliation catches divergence |
| `replay.log` | Full replay of 44 trading days (2025-09-02 to 2025-10-31): S1–S6 in order each day, S7 rules evaluation fires automatically at month ends; September proposes no change, October proposes a drift-band change |
| `recovery.log` | Interruption recovery: a crash is injected after S3 on 2025-09-10 (exit code 75); after restart, S1–S3 are skipped by checkpoints (attempts stay 1) and only S4–S6 resume |
| `approval-flow.log` | Async human approval: S5 pauses cleanly in `waiting` state, the approval request is written to disk and pushed, the `approve` decision resumes the day to completion from checkpoints |
| `reconciliation.log` | Positions reconciliation: inbox snapshot diverges from recorded state → the day stops at S1 → the human books adjustments via `import-positions` → checkpoint resume completes the day |
| `rules-lifecycle.log` | Rules version lifecycle: an S7 proposal becomes v2 after human approval, then rolls back to v1 (recorded as v3; the audit trail is append-only, history is never deleted) |

Example work products (daily reports, proposals, review verdicts, approval
records, fill reconciliation) live in `examples/outputs/`:
`examples/outputs/2025-09-02/` is the full approval-day chain (S3 proposal
+ S4 review + S5 approval + S6 pending order), and
`examples/outputs/2025-09-03/` is next-day execution (T+1 fills with
per-fill approval-price vs fill-price disclosure).
