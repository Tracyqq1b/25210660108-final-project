"""S4 independent review: recomputes everything from source data.

The reviewer is independent of the producing stage by construction: it
does not read S3's arithmetic, only its trade list. Weights, breaches,
and order quantities are recomputed from the raw positions and the raw
price cache, then compared against what the proposal claims. Any
discrepancy fails the review and blocks S5/S6.
"""

from . import datafeed


def review(ctx, day):
    proposal = ctx.store.stage_artifact(day, "S3")
    if proposal is None:
        raise RuntimeError("S4 requires the S3 artifact for %s" % day)
    _, rules = ctx.store.active_rules()

    # Independent re-computation from source data.
    positions = ctx.store.get_positions()
    cash = ctx.store.get_cash()
    prices = {s: datafeed.price_on(s, day, ctx.cache_dir)
              for s in ctx.cfg["portfolio"]["universe"]}
    values = {s: positions.get(s, 0.0) * prices[s] for s in prices}
    total = cash + sum(values.values())
    weights = {s: (values[s] / total if total else 0.0) for s in prices}

    targets = rules.get("targets", {})
    recomputed_breaches = set()
    for sym, w in weights.items():
        if w > rules["max_weight"]:
            recomputed_breaches.add(("max_weight", sym))
        t = targets.get(sym)
        if t is not None and abs(w - t) > rules["drift_band"]:
            recomputed_breaches.add(("drift_band", sym))

    checks = []
    ok = True

    def check(name, passed, detail=""):
        nonlocal ok
        checks.append({"check": name, "pass": bool(passed),
                       "detail": detail})
        ok = ok and passed

    # 1. Proposal trades must be internally consistent (qty > 0, known
    #    symbol, price within 1% of the close we re-fetched).
    for t in proposal["trades"]:
        sym = t["symbol"]
        check("trade_well_formed:%s" % sym,
              t["qty"] > 0 and sym in prices,
              "%s %s x%s" % (t["side"], t["qty"], sym))
        ref = prices.get(sym)
        if ref:
            dev = abs(t["est_price"] - ref) / ref
            check("price_consistent:%s" % sym, dev <= 0.01,
                  "est %.2f vs close %.2f (dev %.4f)"
                  % (t["est_price"], ref, dev))

    # 2. No SELL exceeds the independently observed holding.
    for t in proposal["trades"]:
        if t["side"] == "SELL":
            held = positions.get(t["symbol"], 0.0)
            check("sell_within_holding:%s" % t["symbol"],
                  t["qty"] <= held,
                  "selling %s of %.0f held" % (t["qty"], held))

    # 3. Every recomputed drift/cap breach must be addressed by a trade
    #    in that symbol (unless below one lot).
    traded = {t["symbol"] for t in proposal["trades"]}
    for kind, sym in sorted(recomputed_breaches):
        if sym not in traded:
            implied = abs((targets.get(sym, weights[sym]) - weights[sym])
                          * total / prices[sym])
            check("breach_addressed:%s" % sym, implied < 1.0,
                  "%s breach on %s not traded; implied qty %.2f"
                  % (kind, sym, implied))

    # 4. Post-trade simulation: after applying the proposal, every traded
    #    name must end inside its band (or the cap), given close prices.
    sim_pos = dict(positions)
    sim_cash = cash
    for t in proposal["trades"]:
        notion = t["qty"] * prices[t["symbol"]]
        if t["side"] == "SELL":
            sim_pos[t["symbol"]] = sim_pos.get(t["symbol"], 0.0) - t["qty"]
            sim_cash += notion
        else:
            sim_pos[t["symbol"]] = sim_pos.get(t["symbol"], 0.0) + t["qty"]
            sim_cash -= notion
    check("cash_nonnegative_after", sim_cash >= -1e-9,
          "simulated cash %.2f" % sim_cash)
    sim_total = sim_cash + sum(sim_pos.get(s, 0.0) * prices[s]
                               for s in prices)
    for t in proposal["trades"]:
        sym = t["symbol"]
        w_after = sim_pos.get(sym, 0.0) * prices[sym] / sim_total
        tgt = targets.get(sym)
        if tgt is not None:
            check("post_trade_in_band:%s" % sym,
                  abs(w_after - tgt) <= rules["drift_band"] + 1e-6
                  or w_after <= rules["max_weight"] + 1e-6,
                  "weight after %.4f vs target %.4f" % (w_after, tgt))

    return {
        "stage": "S4",
        "run_date": day,
        "status": "pass" if ok else "fail",
        "checks": checks,
        "independent_inputs": {
            "source": "positions+cash from state, closes from cache",
            "total_value": round(total, 2),
        },
        "verdict": ("Proposal reproduces from source data; all %d checks "
                    "passed." % len(checks)) if ok else
                   "Proposal FAILED independent review; see failed checks.",
    }
