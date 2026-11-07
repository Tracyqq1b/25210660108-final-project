"""Deterministic implementations of workflow stages S1-S6.

Every calculation in the daily pipeline is plain code, not model output:
weights, drawdowns, drift, order quantities, fills, and reconciliation are
recomputed the same way every time. The LLM-agent layer (see prompts/)
only narrates and judges; it never produces a number the pipeline trusts.

Each stage returns its artifact dict; the controller persists it through
StateStore.save_artifact and records the checkpoint.
"""

from . import datafeed


def _prices(ctx, day):
    """Close prices for the whole universe on one trading day."""
    return {s: datafeed.price_on(s, day, ctx.cache_dir)
            for s in ctx.cfg["portfolio"]["universe"]}


def _valuation(ctx, day, prices=None):
    prices = prices or _prices(ctx, day)
    positions = ctx.store.get_positions()
    cash = ctx.store.get_cash()
    values = {s: positions.get(s, 0.0) * prices[s] for s in prices}
    total = cash + sum(values.values())
    weights = {s: (values[s] / total if total else 0.0) for s in prices}
    return {"prices": prices, "positions": positions, "cash": cash,
            "values": values, "total": total, "weights": weights}


# ---------------------------------------------------------------- S1 ----

def s1_ingest(ctx, day):
    """S1 data ingestion: standardized end-of-day market snapshot.

    Reads only rows dated <= ``day`` from the cached feed (the feed itself
    enforces the cutoff), so the snapshot contains exactly what was knowable
    at the close of ``day``.
    """
    prices = _prices(ctx, day)
    ranges = {s: datafeed.cache_range(s, ctx.cache_dir)
              for s in ctx.cfg["portfolio"]["universe"]}
    return {
        "stage": "S1",
        "run_date": day,
        "source": "eastmoney-kline-cache",
        "as_of": day,
        "prices": prices,
        "cache_coverage": ranges,
    }


# ---------------------------------------------------------------- S2 ----

def s2_monitor(ctx, day):
    """S2 risk monitoring: weights, drift, drawdown, sector concentration."""
    _, rules = ctx.store.active_rules()
    val = _valuation(ctx, day)

    history = ctx.store.equity_history()
    peak = max([v for _, v in history] + [val["total"]])
    drawdown = (val["total"] - peak) / peak if peak else 0.0

    sectors = rules.get("sectors", {})
    sector_weights = {}
    for sym, w in val["weights"].items():
        sec = sectors.get(sym, "Other")
        sector_weights[sec] = sector_weights.get(sec, 0.0) + w

    breaches = []
    targets = rules.get("targets", {})
    for sym, w in sorted(val["weights"].items()):
        if w > rules["max_weight"]:
            breaches.append({"rule": "max_weight", "symbol": sym,
                             "value": round(w, 4),
                             "limit": rules["max_weight"]})
        t = targets.get(sym)
        if t is not None and abs(w - t) > rules["drift_band"]:
            breaches.append({"rule": "drift_band", "symbol": sym,
                             "value": round(w, 4), "target": t,
                             "limit": rules["drift_band"]})
    if drawdown < -rules["max_drawdown"]:
        breaches.append({"rule": "max_drawdown", "value": round(drawdown, 4),
                         "limit": rules["max_drawdown"]})
    for sec, w in sorted(sector_weights.items()):
        if w > rules["max_sector_weight"]:
            breaches.append({"rule": "max_sector_weight", "sector": sec,
                             "value": round(w, 4),
                             "limit": rules["max_sector_weight"]})

    ctx.store.record_equity(day, val["total"])

    return {
        "stage": "S2",
        "run_date": day,
        "rule_version": ctx.store.active_rules()[0],
        "total_value": round(val["total"], 2),
        "cash": round(val["cash"], 2),
        "weights": {k: round(v, 4) for k, v in val["weights"].items()},
        "sector_weights": {k: round(v, 4)
                           for k, v in sector_weights.items()},
        "drawdown": round(drawdown, 4),
        "breaches": breaches,
        "breach": bool(breaches),
    }


# ---------------------------------------------------------------- S3 ----

def s3_signal(ctx, day):
    """S3 signal generation: turn breaches into a rebalance proposal.

    Deterministic policy: any name outside its drift band is traded back to
    its target weight; any name above the single-asset cap is trimmed to the
    cap. Sells are listed before buys so the proposal is self-funding.
    """
    _, rules = ctx.store.active_rules()
    risk = ctx.store.stage_artifact(day, "S2")
    if risk is None:
        raise RuntimeError("S3 requires the S2 artifact for %s" % day)
    val = _valuation(ctx, day)
    risk_hash = None

    trades = []
    if risk["breach"]:
        total = val["total"]
        targets = rules.get("targets", {})
        for sym in sorted(val["weights"]):
            w = val["weights"][sym]
            t = targets.get(sym)
            desired_w = None
            if w > rules["max_weight"]:
                desired_w = rules["max_weight"]
            elif t is not None and abs(w - t) > rules["drift_band"]:
                desired_w = t
            if desired_w is None:
                continue
            diff_value = (desired_w - w) * total
            qty = int(abs(diff_value) / val["prices"][sym])
            if qty < 1:
                continue
            trades.append({
                "symbol": sym,
                "side": "SELL" if diff_value < 0 else "BUY",
                "qty": qty,
                "est_price": round(val["prices"][sym], 2),
                "est_notional": round(qty * val["prices"][sym], 2),
                "reason": "weight %.4f -> target %.4f" % (w, desired_w),
            })
        trades.sort(key=lambda t: 0 if t["side"] == "SELL" else 1)

    if not risk["breach"]:
        action = "HOLD"
        rationale = ("No rule breach detected at close of %s; "
                     "no rebalancing required." % day)
    elif trades:
        action = "REBALANCE"
        cited = sorted({b["rule"] for b in risk["breaches"]})
        rationale = ("Breached rules at close of %s: %s. Proposal trades "
                     "the offending names back inside their bands "
                     "(sells first, self-funding)." % (day, ", ".join(cited)))
    else:
        action = "ALERT_ONLY"
        rationale = ("Breaches detected at close of %s but all implied "
                     "trades are below one lot; alert only." % day)

    return {
        "stage": "S3",
        "run_date": day,
        "rule_version": risk["rule_version"],
        "action": action,
        "trades": trades,
        "rationale": rationale,
        "based_on": {"S2_total_value": risk["total_value"],
                     "S2_breaches": risk["breaches"]},
    }


# ---------------------------------------------------------------- S5 ----

def s5_approval(ctx, day, auto=False, note=""):
    """S5 human approval gate.

    Interactive by default: shows the reviewed proposal and waits for the
    user to type approve/reject. ``auto=True`` exists for tests and the
    scripted demo replay, and is always recorded as such in the audit note.
    """
    proposal = ctx.store.stage_artifact(day, "S3")
    review = ctx.store.stage_artifact(day, "S4")
    if proposal is None or review is None:
        raise RuntimeError("S5 requires S3 and S4 artifacts for %s" % day)
    if review["status"] != "pass":
        raise RuntimeError("S5 refuses: S4 review did not pass (%s)"
                           % review["status"])
    proposal_hash = ctx.store.save_artifact(day, "S3", proposal)

    if auto:
        decision = "approved"
        note = (note + " " if note else "") + "AUTO-APPROVED (demo/test mode)"
    elif proposal["action"] == "HOLD":
        decision = "approved"
        note = "no action required (HOLD proposal)"
    else:
        print("=== Rebalance proposal %s ===" % day)
        print(proposal["rationale"])
        for t in proposal["trades"]:
            print("  %(side)s %(qty)s x %(symbol)s @ ~%(est_price)s"
                  " (%(reason)s)" % t)
        answer = input("Approve? [y/N] ").strip().lower()
        decision = "approved" if answer == "y" else "rejected"

    approval_id = ctx.store.record_approval(day, proposal_hash, decision, note)
    return {
        "stage": "S5",
        "run_date": day,
        "approval_id": approval_id,
        "proposal_hash": proposal_hash,
        "decision": decision,
        "note": note,
    }


# ---------------------------------------------------------------- S6 ----

def s6_execute(ctx, day):
    """S6 sandbox execution + reconciliation.

    Hard gate: no fill happens without an 'approved' S5 record whose
    proposal hash matches the current S3 artifact. Fills use the close
    price with configured slippage; each fill is applied atomically.
    """
    proposal = ctx.store.stage_artifact(day, "S3")
    approval = ctx.store.stage_artifact(day, "S5")
    if proposal is None or approval is None:
        raise RuntimeError("S6 requires S3 and S5 artifacts for %s" % day)
    if approval["decision"] != "approved":
        return {"stage": "S6", "run_date": day, "status": "skipped",
                "reason": "proposal not approved", "fills": []}

    proposal_hash = ctx.store.save_artifact(day, "S3", proposal)
    if approval["proposal_hash"] != proposal_hash:
        raise RuntimeError("approval does not match current proposal")
    approval_id = ctx.store.find_approval(day, proposal_hash)
    if approval_id is None:
        raise RuntimeError("no approved record found for proposal")

    max_notional = ctx.cfg["execution"]["max_order_notional"]
    slip = ctx.cfg["execution"]["slippage_bps"] / 1e4
    cash = ctx.store.get_cash()
    positions = ctx.store.get_positions()

    # Pre-flight affordability / inventory check before any fill.
    projected_cash = cash
    projected_pos = dict(positions)
    for t in proposal["trades"]:
        notional = t["qty"] * t["est_price"]
        if notional > max_notional:
            raise RuntimeError("order notional %.2f exceeds cap %.2f"
                               % (notional, max_notional))
        if t["side"] == "SELL":
            if projected_pos.get(t["symbol"], 0.0) < t["qty"]:
                raise RuntimeError("cannot sell %s x%s: not held"
                                   % (t["symbol"], t["qty"]))
            projected_pos[t["symbol"]] -= t["qty"]
            projected_cash += notional
        else:
            projected_cash -= notional
            projected_pos[t["symbol"]] = (
                projected_pos.get(t["symbol"], 0.0) + t["qty"])
    if projected_cash < -1e-9:
        raise RuntimeError("insufficient cash for proposed buys")

    fills = []
    for t in proposal["trades"]:
        price = datafeed.price_on(t["symbol"], day, ctx.cache_dir)
        price *= (1 - slip) if t["side"] == "SELL" else (1 + slip)
        price = round(price, 4)
        trade_id = ctx.store.apply_fill(day, t["symbol"], t["side"],
                                        t["qty"], price, approval_id)
        fills.append({"trade_id": trade_id, "symbol": t["symbol"],
                      "side": t["side"], "qty": t["qty"], "price": price,
                      "notional": round(t["qty"] * price, 2)})

    return {
        "stage": "S6",
        "run_date": day,
        "status": "executed",
        "approval_id": approval_id,
        "fills": fills,
        "reconciliation": {
            "orders": len(proposal["trades"]),
            "fills": len(fills),
            "cash_after": round(ctx.store.get_cash(), 2),
            "positions_after": ctx.store.get_positions(),
        },
    }
