"""Report renderer: turns the day's artifact chain into a human-readable
Markdown report.

The renderer does no arithmetic beyond formatting: every number is copied
from the persisted stage artifacts, and the artifact hashes are printed at
the bottom so the report is auditable back to its inputs. In a deployed
setup the narrative paragraphs are where the LLM layer (prompts/) would
plug in; the skeleton and all figures stay deterministic.
"""


def _fmt_money(v):
    return "${:,.2f}".format(v)


def _pct(v):
    return "{:.2%}".format(v)


def _prev_equity(store, day):
    prev = [(d, v) for d, v in store.equity_history() if d < day]
    return prev[-1] if prev else None


def render_day(store, day):
    """Render the daily end-of-day report for `day`. Returns None if the
    day was never run (no S2 artifact)."""
    s2 = store.stage_artifact(day, "S2")
    if s2 is None:
        return None
    s3 = store.stage_artifact(day, "S3")
    s4 = store.stage_artifact(day, "S4")
    s5 = store.stage_artifact(day, "S5")
    s6 = store.stage_artifact(day, "S6")

    lines = ["# End-of-Day Risk Report %s" % day, ""]
    lines.append("Rules version: v%s" % s2["rule_version"])
    lines.append("")

    prev = _prev_equity(store, day)
    change = ""
    if prev and prev[1]:
        change = " (%+.2f%% vs %s)" % (
            (s2["total_value"] / prev[1] - 1) * 100, prev[0])
    lines.append("## Portfolio overview")
    lines.append("")
    lines.append("- Total value: %s%s" % (_fmt_money(s2["total_value"]),
                                           change))
    lines.append("- Cash: %s" % _fmt_money(s2["cash"]))
    lines.append("- Drawdown: %s (within limit: %s)" % (
        _pct(s2["drawdown"]),
        "yes" if not any(b["rule"] == "max_drawdown" for b in s2["breaches"])
        else "NO"))
    lines.append("")
    lines.append("| Symbol | Weight |")
    lines.append("| --- | --- |")
    for sym, w in sorted(s2["weights"].items()):
        lines.append("| %s | %s |" % (sym, _pct(w)))
    lines.append("")

    lines.append("## Rule breaches")
    lines.append("")
    if s2["breaches"]:
        lines.append("| Rule | Subject | Value | Limit |")
        lines.append("| --- | --- | --- | --- |")
        for b in s2["breaches"]:
            target = b.get("symbol") or b.get("sector") or "portfolio"
            lines.append("| %s | %s | %s | %s |" % (
                b["rule"], target, b.get("value"), b.get("limit")))
    else:
        lines.append("No breaches.")
    lines.append("")

    lines.append("## Action")
    lines.append("")
    if s3 is None:
        lines.append("Signal stage did not run.")
    elif s3["action"] == "HOLD":
        lines.append("**HOLD** — %s" % s3["rationale"])
    else:
        lines.append("**%s** — %s" % (s3["action"], s3["rationale"]))
        if s3["trades"]:
            lines.append("")
            lines.append("| Side | Qty | Symbol | Ref price | Reason |")
            lines.append("| --- | --- | --- | --- | --- |")
            for t in s3["trades"]:
                lines.append("| %s | %d | %s | %.2f | %s |" % (
                    t["side"], t["qty"], t["symbol"], t["est_price"],
                    t["reason"]))
        lines.append("")
        if s4:
            passed = sum(1 for c in s4["checks"] if c["pass"])
            lines.append("- Independent review (S4): **%s** (%d/%d checks "
                         "passed)" % (s4["status"], passed,
                                      len(s4["checks"])))
        if s5:
            lines.append("- Human approval (S5): **%s**%s" % (
                s5["decision"],
                " — " + s5["note"] if s5.get("note") else ""))
    if s6:
        if s6.get("fills"):
            lines.append("- Execution & reconciliation (S6): %d fill(s) "
                         "(T+1 execution); cash after: %s"
                         % (len(s6["fills"]),
                            _fmt_money(s6["reconciliation"]
                                       ["cash_after"])))
            lines.append("")
            lines.append("| Fill | Qty | Symbol | Ref price (approval day) "
                         "| Fill price | Diff | Notional |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for f in s6["fills"]:
                lines.append("| %s | %d | %s | %.2f | %.4f | %+.4f | %s |"
                             % (f["side"], f["qty"], f["symbol"],
                                f["est_price"], f["price"],
                                f["price_deviation"],
                                _fmt_money(f["notional"])))
        else:
            lines.append("- Execution & reconciliation (S6): no fills today")
        if s6.get("new_pending_orders"):
            lines.append("- %d new pending order(s) placed, filling next "
                         "trading day: %s" % (
                             len(s6["new_pending_orders"]),
                             "; ".join("%s %d x %s" % (
                                 o["side"], o["qty"], o["symbol"])
                                 for o in s6["new_pending_orders"])))
    lines.append("")

    lines.append("## Audit chain (artifact hashes)")
    lines.append("")
    for stage in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        row = store.conn.execute(
            "SELECT artifact_hash, status FROM stage_runs"
            " WHERE run_date = ? AND stage = ?", (day, stage)).fetchone()
        if row:
            lines.append("- %s: %s (%s)" % (stage, row["artifact_hash"],
                                            row["status"]))
    lines.append("")
    return "\n".join(lines)


def render_approval_request(store, day):
    """Render the approval-request document for a pending proposal. This is
    what the operator reads before running `approve`/`approve --reject`."""
    s3 = store.stage_artifact(day, "S3")
    s4 = store.stage_artifact(day, "S4")
    s2 = store.stage_artifact(day, "S2")
    lines = ["# Approval Request %s" % day, ""]
    lines.append("**Action**: %s (rules version v%s)" % (s3["action"],
                                                         s3["rule_version"]))
    lines.append("")
    lines.append("**Rationale**: %s" % s3["rationale"])
    lines.append("")
    if s2 and s2["breaches"]:
        lines.append("## Triggering breaches")
        lines.append("")
        lines.append("| Rule | Subject | Value | Limit |")
        lines.append("| --- | --- | --- | --- |")
        for b in s2["breaches"]:
            target = b.get("symbol") or b.get("sector") or "portfolio"
            lines.append("| %s | %s | %s | %s |" % (
                b["rule"], target, b.get("value"), b.get("limit")))
        lines.append("")
    lines.append("## Proposed trades (filled T+1)")
    lines.append("")
    lines.append("| Side | Qty | Symbol | Ref price | Est notional "
                 "| Reason |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for t in s3["trades"]:
        lines.append("| %s | %d | %s | %.2f | %s | %s |" % (
            t["side"], t["qty"], t["symbol"], t["est_price"],
            _fmt_money(t["est_notional"]), t["reason"]))
    lines.append("")
    if s4:
        passed = sum(1 for c in s4["checks"] if c["pass"])
        lines.append("## Independent review (S4)")
        lines.append("")
        lines.append("Verdict: **%s** (%d/%d checks passed)" % (
            s4["status"], passed, len(s4["checks"])))
        for c in s4["checks"]:
            if not c["pass"]:
                lines.append("- FAILED: %s — %s" % (c["check"],
                                                    c["detail"]))
        lines.append("")
    lines.append("## How to decide")
    lines.append("")
    lines.append("```sh")
    lines.append("python3 src/pma/cli.py approve --day %s          "
                 "# approve" % day)
    lines.append("python3 src/pma/cli.py approve --day %s --reject "
                 "# reject" % day)
    lines.append("```")
    lines.append("")
    lines.append("On approval the run resumes automatically: orders become "
                 "pending and fill on the next trading day with full "
                 "reconciliation.")
    lines.append("")
    return "\n".join(lines)


def render_month(store, month):
    """Render the S7 monthly rules-evaluation report. Returns None if S7
    has not run for that month."""
    rows = store.conn.execute(
        "SELECT artifact_hash FROM stage_runs WHERE stage = 'S7'"
        " AND status = 'done' AND run_date LIKE ? ORDER BY run_date DESC",
        (month + "-%",)).fetchall()
    if not rows:
        return None
    s7 = store.get_artifact(rows[0]["artifact_hash"])

    lines = ["# Monthly Rules Evaluation Report %s" % month, ""]
    lines.append("Window: %s to %s (%d trading days); rules version "
                 "evaluated: v%s" % (
                     s7["window"]["start"], s7["window"]["end"],
                     s7["window"]["days"], s7["rule_version_evaluated"]))
    lines.append("")
    lines.append("## Fixed-baseline replay comparison")
    lines.append("")
    lines.append("| Scenario | End value | Max drawdown | Turnover "
                 "| Breach days | Rebalance days |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name, m in s7["metrics"].items():
        lines.append("| %s | %s | %s | %s | %d | %d |" % (
            name, _fmt_money(m["end_value"]), _pct(m["max_drawdown"]),
            _fmt_money(m["turnover"]), m["breach_days"],
            m["rebalance_days"]))
    lines.append("")
    lines.append("## Conclusion")
    lines.append("")
    if s7["proposal"]:
        lines.append("**Parameter-change proposal**: %s"
                     % s7["proposal"]["reason"])
        lines.append("")
        lines.append("The proposal takes effect only after human approval "
                     "(`apply-rules`); if rejected, the current rules stay "
                     "in force.")
    else:
        lines.append("No parameter change recommended this month: no "
                     "candidate met both admission criteria (end value at "
                     "least the buy-and-hold baseline, and strictly fewer "
                     "breach days than the current rules).")
    lines.append("")
    return "\n".join(lines)
