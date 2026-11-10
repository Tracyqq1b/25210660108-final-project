"""Deterministic implementations of workflow stages S1-S6.

Every calculation in the daily pipeline is plain code, not model output:
weights, drawdowns, drift, order quantities, fills, and reconciliation are
recomputed the same way every time. The LLM-agent layer (see prompts/)
only narrates and judges; it never produces a number the pipeline trusts.

Each stage returns its artifact dict; the controller persists it through
StateStore.save_artifact and records the checkpoint.
"""

import csv
import math
import os

from . import datafeed


class PositionsDivergedError(RuntimeError):
    """Raised when an external positions snapshot disagrees with recorded
    state. The pipeline stops and escalates; a human resolves it with
    `import-positions`, then the day resumes from S1."""


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

def load_positions_snapshot(path):
    """Parse a broker positions export: CSV rows of ``symbol,qty`` plus an
    optional ``CASH,<amount>`` row. Returns (positions, cash_or_None)."""
    positions = {}
    cash = None
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].strip().startswith("#"):
                continue
            symbol, qty = row[0].strip().upper(), float(row[1])
            if symbol == "CASH":
                cash = qty
            else:
                positions[symbol] = qty
    return positions, cash


def s1_ingest(ctx, day):
    """S1 data ingestion: standardized end-of-day market snapshot, plus
    positions reconciliation when a broker snapshot is in the inbox.

    Reads only rows dated <= ``day`` from the cached feed (the feed itself
    enforces the cutoff), so the snapshot contains exactly what was knowable
    at the close of ``day``. If ``<inbox>/<day>_positions.csv`` exists, the
    recorded positions must match it; a mismatch stops the pipeline and
    escalates (resolve with `import-positions`, then resume)."""
    prices = _prices(ctx, day)
    ranges = {s: datafeed.cache_range(s, ctx.cache_dir)
              for s in ctx.cfg["portfolio"]["universe"]}
    artifact = {
        "stage": "S1",
        "run_date": day,
        "source": "eastmoney-kline-cache",
        "as_of": day,
        "prices": prices,
        "cache_coverage": ranges,
    }

    inbox = getattr(ctx, "inbox_dir", None)
    if inbox:
        snapshot_path = os.path.join(inbox, "%s_positions.csv" % day)
        if os.path.exists(snapshot_path):
            snapshot, cash = load_positions_snapshot(snapshot_path)
            diffs = ctx.store.diff_positions(snapshot, cash)
            artifact["positions_reconciliation"] = {
                "snapshot": snapshot_path,
                "status": "ok" if not diffs else "diverged",
                "diffs": diffs,
            }
            if diffs:
                raise PositionsDivergedError(
                    "positions snapshot %s diverges from recorded state: %s"
                    " -- resolve with `import-positions` and resume"
                    % (snapshot_path, diffs))
    return artifact


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

    # Orders approved earlier fill on the next trading day; a breach on a
    # symbol with an in-flight order is already being addressed, so no
    # second order is placed for it.
    in_flight = {}
    for o in ctx.store.get_pending_orders():
        in_flight[o["symbol"]] = o

    trades = []
    deferred = []
    if risk["breach"]:
        total = val["total"]
        targets = rules.get("targets", {})
        for sym in sorted(val["weights"]):
            w = val["weights"][sym]
            t = targets.get(sym)
            desired_w = None
            trim_to_cap = False
            if w > rules["max_weight"]:
                desired_w = rules["max_weight"]
                trim_to_cap = True
            elif t is not None and abs(w - t) > rules["drift_band"]:
                desired_w = t
            if desired_w is None:
                continue
            if sym in in_flight:
                deferred.append({
                    "symbol": sym,
                    "order_id": in_flight[sym]["id"],
                    "reason": "breach already addressed by pending order "
                              "#%d (%s %d, approved %s)" % (
                                  in_flight[sym]["id"],
                                  in_flight[sym]["side"],
                                  int(in_flight[sym]["qty"]),
                                  in_flight[sym]["created_date"]),
                })
                continue
            diff_value = (desired_w - w) * total
            # sells round up so a cap trim actually lands under the cap;
            # buys round down so they never exceed the intended notional
            if diff_value < 0:
                qty = math.ceil(abs(diff_value) / val["prices"][sym])
            else:
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
    elif deferred:
        action = "HOLD"
        rationale = ("Breaches detected at close of %s, but every affected "
                     "name already has an in-flight order filling next "
                     "trading day; no new orders." % day)
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
        "deferred": deferred,
        "rationale": rationale,
        "based_on": {"S2_total_value": risk["total_value"],
                     "S2_breaches": risk["breaches"]},
    }


# ---------------------------------------------------------------- S5 ----

class AwaitingApproval(Exception):
    """Raised by S5 when a consequential proposal has no human decision
    yet. The controller marks the stage 'waiting' and pauses the day
    cleanly; the run resumes once the operator records a decision with
    the `approve` command."""


def s5_approval(ctx, day, auto=False, note=""):
    """S5 human approval gate, asynchronous.

    HOLD proposals are acknowledged automatically. Actionable proposals
    require a recorded human decision: on first arrival the stage writes
    an approval-request document, pushes a notification through the
    configured channels, and pauses. The `approve` command records the
    decision; on resume this stage finds the record and completes.
    """
    from . import notify, render

    proposal = ctx.store.stage_artifact(day, "S3")
    review = ctx.store.stage_artifact(day, "S4")
    if proposal is None or review is None:
        raise RuntimeError("S5 requires S3 and S4 artifacts for %s" % day)
    if review["status"] != "pass":
        raise RuntimeError("S5 refuses: S4 review did not pass (%s)"
                           % review["status"])
    proposal_hash = ctx.store.save_artifact(day, "S3", proposal)

    if proposal["action"] == "HOLD" and not proposal["trades"]:
        ctx.store.record_approval(day, proposal_hash, "approved",
                                  "no action required (HOLD proposal)")
        decision = "approved"
        note = "no action required (HOLD proposal)"
    elif auto:
        ctx.store.record_approval(day, proposal_hash, "approved",
                                  (note + " " if note else "")
                                  + "AUTO-APPROVED (demo/test mode)")
        decision = "approved"
        note = "AUTO-APPROVED (demo/test mode)"
    else:
        existing = ctx.store.find_approval_any(day, proposal_hash)
        if existing is None:
            doc = render.render_approval_request(ctx.store, day)
            if getattr(ctx, "approvals_dir", None):
                import os
                os.makedirs(ctx.approvals_dir, exist_ok=True)
                with open(os.path.join(ctx.approvals_dir,
                                       "%s.md" % day), "w") as fh:
                    fh.write(doc)
            notify.send(
                ctx.cfg,
                "[PMA] Rebalance proposal awaiting approval — %s" % day,
                "%s; %d trade(s) pending your decision. Decide with: "
                "python3 src/pma/cli.py approve --day %s"
                % (proposal["action"], len(proposal["trades"]), day))
            raise AwaitingApproval(day)
        decision = existing["decision"]
        note = note or "operator decision via approve command"

    approval_id = ctx.store.find_approval_any(day, proposal_hash)["id"]
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
    """S6 sandbox execution + reconciliation, on a T+1 model.

    Phase 1: fill orders that were approved on earlier days, at *today's*
    price -- a decision made with T-day close data can never fill at the
    T-day close, so approval creates a pending order and the next trading
    day's S6 fills it. Phase 2: today's approved proposal becomes pending
    orders for the next trading day.

    Hard gate: pending orders are created only when an 'approved' S5 record
    matches the current proposal hash. Quantities always match the approved
    proposal; the fill-price difference vs the T-day estimate is disclosed
    in the reconciliation.
    """
    slip = ctx.cfg["execution"]["slippage_bps"] / 1e4
    max_notional = ctx.cfg["execution"]["max_order_notional"]

    # ---- phase 1: fill pending orders from earlier approvals
    fills = []
    pending = ctx.store.get_pending_orders()
    pending.sort(key=lambda o: 0 if o["side"] == "SELL" else 1)
    for order in pending:
        price = datafeed.price_on(order["symbol"], day, ctx.cache_dir)
        price *= (1 - slip) if order["side"] == "SELL" else (1 + slip)
        price = round(price, 4)
        notional = order["qty"] * price
        if notional > max_notional:
            raise RuntimeError("pending order #%s notional %.2f exceeds cap"
                               % (order["id"], notional))
        if order["side"] == "SELL":
            if ctx.store.get_positions().get(order["symbol"], 0.0) \
                    < order["qty"]:
                raise RuntimeError(
                    "cannot fill pending SELL %s x%s: insufficient holding"
                    " (escalate: resolve externally and reconcile)"
                    % (order["symbol"], order["qty"]))
        elif notional > ctx.store.get_cash():
            raise RuntimeError(
                "cannot fill pending BUY %s x%s: insufficient cash"
                " (escalate: resolve externally and reconcile)"
                % (order["symbol"], order["qty"]))
        trade_id = ctx.store.fill_pending_order(order["id"], day, price)
        fills.append({"trade_id": trade_id, "order_id": order["id"],
                      "symbol": order["symbol"], "side": order["side"],
                      "qty": order["qty"], "price": price,
                      "notional": round(notional, 2),
                      "est_price": order["est_price"],
                      "approved_on": order["created_date"],
                      "price_deviation": round(price - order["est_price"],
                                               4)})

    # ---- phase 2: today's approved proposal -> pending orders
    new_orders = []
    proposal = ctx.store.stage_artifact(day, "S3")
    approval = ctx.store.stage_artifact(day, "S5")
    if proposal and proposal["trades"]:
        if approval is None:
            raise RuntimeError("proposal with trades has no S5 approval")
        if approval["decision"] != "approved":
            pass  # rejected: nothing to place
        else:
            proposal_hash = ctx.store.save_artifact(day, "S3", proposal)
            if approval["proposal_hash"] != proposal_hash:
                raise RuntimeError(
                    "approval does not match current proposal")
            approval_id = ctx.store.find_approval(day, proposal_hash)
            if approval_id is None:
                raise RuntimeError("no approved record found for proposal")

            # Pre-flight inventory/cash check before placing any order.
            projected_cash = ctx.store.get_cash()
            projected_pos = ctx.store.get_positions()
            for t in proposal["trades"]:
                if t["est_notional"] > max_notional:
                    raise RuntimeError(
                        "order notional %.2f exceeds cap %.2f"
                        % (t["est_notional"], max_notional))
                if t["side"] == "SELL":
                    if projected_pos.get(t["symbol"], 0.0) < t["qty"]:
                        raise RuntimeError("cannot sell %s x%s: not held"
                                           % (t["symbol"], t["qty"]))
                    projected_pos[t["symbol"]] -= t["qty"]
                    projected_cash += t["est_notional"]
                else:
                    projected_cash -= t["est_notional"]
                    projected_pos[t["symbol"]] = (
                        projected_pos.get(t["symbol"], 0.0) + t["qty"])
            if projected_cash < -1e-9:
                raise RuntimeError("insufficient cash for proposed buys")

            for t in proposal["trades"]:
                oid = ctx.store.create_pending_order(
                    day, t["symbol"], t["side"], t["qty"], t["est_price"],
                    approval_id)
                new_orders.append({"order_id": oid, "symbol": t["symbol"],
                                   "side": t["side"], "qty": t["qty"],
                                   "est_price": t["est_price"],
                                   "est_notional": t["est_notional"]})

    return {
        "stage": "S6",
        "run_date": day,
        "status": "executed",
        "fills": fills,
        "new_pending_orders": new_orders,
        "reconciliation": {
            "fills_today": len(fills),
            "orders_placed_for_next_day": len(new_orders),
            "cash_after": round(ctx.store.get_cash(), 2),
            "positions_after": ctx.store.get_positions(),
        },
    }
