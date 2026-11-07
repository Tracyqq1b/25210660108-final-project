"""S7 monthly rules evaluation.

Evaluates how the active monitoring rules performed over a calendar month
and proposes parameter changes. A proposal is only ever a proposal: it
carries replay evidence against a fixed baseline, and becomes active rules
only after explicit human approval (see cli.py apply-rules), which writes
a new rule version. Rollback re-activates any earlier version.

Anti-leakage: the portfolio state at the window start is reconstructed by
reversing the recorded fills, and every simulation day reads prices with
as_of = that day, so no simulation sees the future.
"""

from . import datafeed

CANDIDATE_BANDS = [0.03, 0.05, 0.08]


def reconstruct_start_state(ctx, window_days):
    """Exact portfolio state at the start of the window, reconstructed by
    reversing the recorded fills inside the window."""
    positions = ctx.store.get_positions()
    cash = ctx.store.get_cash()
    days = set(window_days)
    for t in ctx.store.get_trades():
        if t["run_date"] not in days:
            continue
        if t["side"] == "BUY":
            positions[t["symbol"]] = positions.get(t["symbol"], 0.0) - t["qty"]
            cash += t["notional"]
        else:
            positions[t["symbol"]] = positions.get(t["symbol"], 0.0) + t["qty"]
            cash -= t["notional"]
    return {k: v for k, v in positions.items() if abs(v) > 1e-9}, cash


def simulate(ctx, days, params, positions, cash):
    """Replay the deterministic drift-band policy over `days` with the
    given rule parameters, starting from (positions, cash)."""
    positions = dict(positions)
    cash = float(cash)
    slip = ctx.cfg["execution"]["slippage_bps"] / 1e4
    universe = ctx.cfg["portfolio"]["universe"]
    targets = params.get("targets", {})

    peak = None
    max_dd = 0.0
    turnover = 0.0
    breach_days = 0
    rebalance_days = 0

    for day in days:
        prices = {s: datafeed.price_on(s, day, ctx.cache_dir)
                  for s in universe}
        values = {s: positions.get(s, 0.0) * prices[s] for s in universe}
        total = cash + sum(values.values())
        weights = {s: values[s] / total for s in universe}

        breached = any(
            w > params["max_weight"]
            or (targets.get(s) is not None
                and abs(w - targets[s]) > params["drift_band"])
            for s, w in weights.items())
        if breached:
            breach_days += 1

        traded = False
        for sym in sorted(universe):
            w = weights[sym]
            t = targets.get(sym)
            desired = None
            if w > params["max_weight"]:
                desired = params["max_weight"]
            elif t is not None and abs(w - t) > params["drift_band"]:
                desired = t
            if desired is None:
                continue
            diff = (desired - w) * total
            qty = int(abs(diff) / prices[sym])
            if qty < 1:
                continue
            if diff < 0:
                price = prices[sym] * (1 - slip)
                positions[sym] -= qty
                cash += qty * price
            else:
                price = prices[sym] * (1 + slip)
                if qty * price > cash:
                    continue
                positions[sym] = positions.get(sym, 0.0) + qty
                cash -= qty * price
            turnover += qty * price
            traded = True
        if traded:
            rebalance_days += 1

        total = cash + sum(positions.get(s, 0.0) * prices[s]
                           for s in universe)
        peak = total if peak is None else max(peak, total)
        if peak:
            max_dd = min(max_dd, (total - peak) / peak)

    return {
        "end_value": round(total, 2),
        "max_drawdown": round(max_dd, 4),
        "turnover": round(turnover, 2),
        "breach_days": breach_days,
        "rebalance_days": rebalance_days,
    }


def evaluate(ctx, month):
    """Evaluate active rules over one month ('YYYY-MM') and build an S7
    artifact with metrics and, when warranted, a change proposal."""
    days = datafeed.trading_days(month + "-01", month + "-31", ctx.cache_dir)
    if not days:
        raise RuntimeError("no trading days in %s" % month)
    version, params = ctx.store.active_rules()
    start_pos, start_cash = reconstruct_start_state(ctx, days)

    no_rebalance = dict(params, drift_band=99.0, max_weight=99.0)
    results = {
        "baseline_buy_hold": simulate(ctx, days, no_rebalance,
                                      start_pos, start_cash),
        "current_rules_v%d" % version: simulate(ctx, days, params,
                                                start_pos, start_cash),
    }

    current_key = "current_rules_v%d" % version
    current = results[current_key]
    baseline = results["baseline_buy_hold"]

    best = None
    for band in CANDIDATE_BANDS:
        if abs(band - params["drift_band"]) < 1e-12:
            continue
        cand_params = dict(params, drift_band=band)
        key = "candidate_drift_band_%.2f" % band
        results[key] = simulate(ctx, days, cand_params,
                                start_pos, start_cash)
        r = results[key]
        eligible = r["end_value"] >= baseline["end_value"]
        score = (r["breach_days"], r["turnover"])
        if eligible and (best is None or score < best[0]):
            best = (score, band, cand_params, key)

    proposal = None
    if best and (best[3] and
                 results[best[3]]["breach_days"] < current["breach_days"]):
        proposal = {
            "change": {"drift_band": best[1]},
            "new_params": best[2],
            "reason": ("drift_band %.2f would have produced %d breach days "
                       "vs %d under current rules on the fixed %s baseline "
                       "replay, with turnover %.2f vs %.2f and end value "
                       "%.2f vs %.2f" % (
                           best[1], results[best[3]]["breach_days"],
                           current["breach_days"], month,
                           results[best[3]]["turnover"], current["turnover"],
                           results[best[3]]["end_value"],
                           current["end_value"])),
            "evidence_key": best[3],
        }

    return {
        "stage": "S7",
        "run_date": days[-1],
        "month": month,
        "rule_version_evaluated": version,
        "window": {"start": days[0], "end": days[-1], "days": len(days)},
        "start_state": {"positions": start_pos,
                        "cash": round(start_cash, 2)},
        "metrics": results,
        "proposal": proposal,
    }
