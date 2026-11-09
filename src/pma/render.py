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

    lines = ["# 日终风险报告 %s" % day, ""]
    lines.append("规则版本:v%s" % s2["rule_version"])
    lines.append("")

    prev = _prev_equity(store, day)
    change = ""
    if prev and prev[1]:
        change = " (%+.2f%% 较 %s)" % (
            (s2["total_value"] / prev[1] - 1) * 100, prev[0])
    lines.append("## 组合概览")
    lines.append("")
    lines.append("- 净值:%s%s" % (_fmt_money(s2["total_value"]), change))
    lines.append("- 现金:%s" % _fmt_money(s2["cash"]))
    lines.append("- 回撤:%s(阈值内:%s)" % (
        _pct(s2["drawdown"]),
        "是" if not any(b["rule"] == "max_drawdown" for b in s2["breaches"])
        else "否"))
    lines.append("")
    lines.append("| 标的 | 权重 |")
    lines.append("| --- | --- |")
    for sym, w in sorted(s2["weights"].items()):
        lines.append("| %s | %s |" % (sym, _pct(w)))
    lines.append("")

    lines.append("## 越界告警")
    lines.append("")
    if s2["breaches"]:
        lines.append("| 规则 | 对象 | 当前值 | 阈值 |")
        lines.append("| --- | --- | --- | --- |")
        for b in s2["breaches"]:
            target = b.get("symbol") or b.get("sector") or "组合"
            lines.append("| %s | %s | %s | %s |" % (
                b["rule"], target, b.get("value"), b.get("limit")))
    else:
        lines.append("无越界。")
    lines.append("")

    lines.append("## 处置")
    lines.append("")
    if s3 is None:
        lines.append("信号阶段未运行。")
    elif s3["action"] == "HOLD":
        lines.append("**HOLD** — %s" % s3["rationale"])
    else:
        lines.append("**%s** — %s" % (s3["action"], s3["rationale"]))
        if s3["trades"]:
            lines.append("")
            lines.append("| 方向 | 数量 | 标的 | 参考价 | 理由 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for t in s3["trades"]:
                lines.append("| %s | %d | %s | %.2f | %s |" % (
                    t["side"], t["qty"], t["symbol"], t["est_price"],
                    t["reason"]))
        lines.append("")
        if s4:
            passed = sum(1 for c in s4["checks"] if c["pass"])
            lines.append("- 独立复核(S4):**%s**(%d/%d 项检查通过)"
                         % (s4["status"], passed, len(s4["checks"])))
        if s5:
            lines.append("- 人工审批(S5):**%s**%s" % (
                s5["decision"],
                " — " + s5["note"] if s5.get("note") else ""))
    if s6:
        if s6.get("fills"):
            lines.append("- 执行对账(S6):%d 笔成交(T+1 执行),执行后现金 %s"
                         % (len(s6["fills"]),
                            _fmt_money(s6["reconciliation"]
                                       ["cash_after"])))
            lines.append("")
            lines.append("| 成交 | 数量 | 标的 | 批准日参考价 | 成交价 | 价差 | 金额 |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for f in s6["fills"]:
                lines.append("| %s | %d | %s | %.2f | %.4f | %+.4f | %s |"
                             % (f["side"], f["qty"], f["symbol"],
                                f["est_price"], f["price"],
                                f["price_deviation"],
                                _fmt_money(f["notional"])))
        else:
            lines.append("- 执行对账(S6):今日无成交")
        if s6.get("new_pending_orders"):
            lines.append("- 新挂待执行订单 %d 笔(次日成交):%s" % (
                len(s6["new_pending_orders"]),
                "; ".join("%s %d×%s" % (o["side"], o["qty"], o["symbol"])
                          for o in s6["new_pending_orders"])))
    lines.append("")

    lines.append("## 审计链(artifact 哈希)")
    lines.append("")
    for stage in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        row = store.conn.execute(
            "SELECT artifact_hash, status FROM stage_runs"
            " WHERE run_date = ? AND stage = ?", (day, stage)).fetchone()
        if row:
            lines.append("- %s:%s(%s)" % (stage, row["artifact_hash"],
                                          row["status"]))
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

    lines = ["# 月度规则评估报告 %s" % month, ""]
    lines.append("评估窗口:%s 至 %s(%d 个交易日),被评估规则版本:v%s" % (
        s7["window"]["start"], s7["window"]["end"], s7["window"]["days"],
        s7["rule_version_evaluated"]))
    lines.append("")
    lines.append("## 固定基线回放对比")
    lines.append("")
    lines.append("| 口径 | 期末净值 | 最大回撤 | 换手 | 越界天数 | 再平衡天数 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name, m in s7["metrics"].items():
        lines.append("| %s | %s | %s | %s | %d | %d |" % (
            name, _fmt_money(m["end_value"]), _pct(m["max_drawdown"]),
            _fmt_money(m["turnover"]), m["breach_days"],
            m["rebalance_days"]))
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    if s7["proposal"]:
        lines.append("**改参提案**:%s" % s7["proposal"]["reason"])
        lines.append("")
        lines.append("提案需经人工批准后生效(`apply-rules`);拒绝则维持"
                     "当前规则。")
    else:
        lines.append("本月不建议改参:没有候选参数同时满足\"期末净值不低于"
                     "买入持有基线且越界天数严格更少\"的入选条件。")
    lines.append("")
    return "\n".join(lines)
