#!/usr/bin/env python3
"""PMA workflow CLI.

Usage examples:
    python3 src/pma/cli.py init
    python3 src/pma/cli.py refresh --start 2024-09-01 --end 2025-10-31
    python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-09-30 --auto-approve
    python3 src/pma/cli.py run-day 2025-09-05
    python3 src/pma/cli.py rules-eval --month 2025-09
    python3 src/pma/cli.py apply-rules --yes
    python3 src/pma/cli.py rollback-rules --to 1 --yes
    python3 src/pma/cli.py status
    python3 src/pma/cli.py export --day 2025-09-05 --out examples/outputs
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from pma import controller, datafeed, render, rules_eval, stages  # noqa: E402
from pma.state import StateStore  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_CONFIG = os.path.join(ROOT, "src", "config.yaml")
DEFAULT_DB = os.path.join(ROOT, "runs", "state.db")

log = logging.getLogger("pma.cli")


def build_ctx(args):
    with open(args.config) as fh:
        cfg = yaml.safe_load(fh)
    store = StateStore(args.db)
    inbox = cfg.get("positions_inbox")
    if inbox and not os.path.isabs(inbox):
        inbox = os.path.join(ROOT, inbox)
    return controller.Context(
        store, cfg, cache_dir=args.cache, inbox_dir=inbox,
        approvals_dir=os.path.join(
            os.path.dirname(os.path.abspath(args.db)), "approvals"),
        auto_approve=getattr(args, "auto_approve", False),
        crash_after=getattr(args, "crash_after", None),
        crash_on=getattr(args, "crash_on", None))


def cmd_init(args):
    ctx = build_ctx(args)
    init = ctx.cfg["initial_state"]
    ctx.store.initialize(init["cash"], init["positions"], ctx.cfg["rules"])
    print("initialized state at %s (as_of %s, cash %.2f, %d positions)"
          % (args.db, init["as_of"], init["cash"], len(init["positions"])))


def cmd_refresh(args):
    ctx = build_ctx(args)
    symbols = args.symbols or ctx.cfg["portfolio"]["universe"]
    datafeed.refresh_cache(symbols, args.start, args.end, args.cache)
    print("cached %d symbols into %s" % (len(symbols), args.cache))


def cmd_import_positions(args):
    ctx = build_ctx(args)
    snapshot, cash = stages.load_positions_snapshot(args.file)
    diffs = ctx.store.diff_positions(snapshot, cash)
    if not diffs:
        print("positions already match recorded state; nothing to do")
        return
    print("discrepancies vs recorded state:")
    for d in diffs:
        print("  %(symbol)s: recorded %(recorded)s -> external %(external)s"
              % d)
    if not args.yes:
        if input("Record as outside-workflow adjustments? [y/N] "
                 ).strip().lower() != "y":
            print("not applied; S1 will keep failing on this snapshot")
            return
    for d in diffs:
        if d["symbol"] == "CASH":
            ctx.store.apply_adjustment(
                args.day, "cash", cash_delta=d["cash_delta"],
                note="reconciled from %s" % args.file)
        else:
            ctx.store.apply_adjustment(
                args.day, "external_trade", symbol=d["symbol"],
                qty_delta=d["qty_delta"],
                note="reconciled from %s" % args.file)
    print("recorded %d adjustment(s) on %s; resume the day to pass S1"
          % (len(diffs), args.day))


def cmd_run_day(args):
    ctx = build_ctx(args)
    _, paused = controller.run_day(ctx, args.date)
    if paused:
        print("run-day %s PAUSED: proposal awaiting approval" % args.date)
        print("review runs/approvals/%s.md, then:" % args.date)
        print("  python3 src/pma/cli.py approve --day %s [--reject]"
              % args.date)
    else:
        print("run-day %s complete" % args.date)


def cmd_approve(args):
    ctx = build_ctx(args)
    proposal = ctx.store.stage_artifact(args.day, "S3")
    if proposal is None:
        print("no proposal for %s" % args.day)
        return
    proposal_hash = ctx.store.save_artifact(args.day, "S3", proposal)
    if ctx.store.find_approval_any(args.day, proposal_hash):
        print("%s already has a recorded decision" % args.day)
        return
    decision = "rejected" if args.reject else "approved"
    ctx.store.record_approval(
        args.day, proposal_hash, decision,
        args.note or "operator decision via approve command")
    print("%s: %s" % (args.day, decision))
    if decision == "approved":
        _, paused = controller.run_day(ctx, args.day)
        print("run-day %s %s" % (args.day,
                                 "complete" if not paused else "paused"))


def _reports_dir(args):
    path = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                        "reports")
    os.makedirs(path, exist_ok=True)
    return path


def cmd_replay(args):
    ctx = build_ctx(args)
    days = datafeed.trading_days(args.start, args.end, args.cache)
    print("replaying %d trading days %s..%s" % (len(days), days[0], days[-1]))
    for day in days:
        try:
            _, paused = controller.run_day(ctx, day)
        except controller.SimulatedCrash as exc:
            print("INTERRUPTED: %s" % exc)
            print("resume with: python3 src/pma/cli.py replay --from %s "
                  "--to %s --db %s" % (day, args.end, args.db))
            sys.exit(75)
        if paused:
            print("PAUSED at %s: proposal awaiting approval." % day)
            print("review runs/approvals/%s.md, then:" % day)
            print("  python3 src/pma/cli.py approve --day %s [--reject]"
                  % day)
            print("then re-run the same replay command to continue.")
            sys.exit(0)
        report = render.render_day(ctx.store, day)
        if report:
            with open(os.path.join(_reports_dir(args), "%s.md" % day),
                      "w") as fh:
                fh.write(report)
        _maybe_rules_eval(ctx, args, day, days)
    print("replay complete; daily reports in %s" % _reports_dir(args))


def _maybe_rules_eval(ctx, args, day, days):
    if not args.with_rules_eval:
        return
    idx = days.index(day)
    is_month_end = idx == len(days) - 1 or days[idx + 1][0:7] != day[0:7]
    if is_month_end and ctx.store.stage_status(day, "S7") != "done":
        artifact = rules_eval.evaluate(ctx, day[0:7])
        digest = ctx.store.save_artifact(day, "S7", artifact)
        ctx.store.begin_stage(day, "S7")
        ctx.store.complete_stage(day, "S7", digest)
        report = render.render_month(ctx.store, day[0:7])
        if report:
            with open(os.path.join(_reports_dir(args),
                                   "%s_rules.md" % day[0:7]), "w") as fh:
                fh.write(report)
        verdict = ("proposal: %s" % artifact["proposal"]["reason"]
                   if artifact["proposal"] else "no change proposed")
        print("S7 rules evaluation %s: %s" % (day[0:7], verdict))


def cmd_report(args):
    ctx = build_ctx(args)
    if args.month:
        text = render.render_month(ctx.store, args.month)
        default = "%s_rules.md" % args.month
    else:
        text = render.render_day(ctx.store, args.day)
        default = "%s.md" % args.day
    if text is None:
        print("nothing to render (day not run / S7 missing)")
        return
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, default)
        with open(path, "w") as fh:
            fh.write(text)
        print("written %s" % path)
    else:
        print(text)


def cmd_rules_eval(args):
    ctx = build_ctx(args)
    artifact = rules_eval.evaluate(ctx, args.month)
    day = artifact["run_date"]
    digest = ctx.store.save_artifact(day, "S7", artifact)
    ctx.store.begin_stage(day, "S7")
    ctx.store.complete_stage(day, "S7", digest)
    print(json.dumps(artifact, indent=2))


def _latest_s7(ctx):
    rows = ctx.store.conn.execute(
        "SELECT run_date, artifact_hash FROM stage_runs WHERE stage = 'S7'"
        " AND status = 'done' ORDER BY run_date DESC").fetchall()
    for row in rows:
        art = ctx.store.get_artifact(row["artifact_hash"])
        if art and art.get("proposal"):
            return art
    return None


def cmd_apply_rules(args):
    ctx = build_ctx(args)
    s7 = _latest_s7(ctx)
    if s7 is None:
        print("no S7 proposal pending")
        return
    print("S7 proposal (%s): %s" % (s7["month"], s7["proposal"]["reason"]))
    if not args.yes:
        if input("Apply as new rule version? [y/N] ").strip().lower() != "y":
            print("not applied")
            return
    version = ctx.store.add_rule_version(
        s7["proposal"]["new_params"],
        "S7 %s: %s" % (s7["month"], s7["proposal"]["change"]))
    digest = ctx.store.save_artifact(s7["run_date"], "S7", s7)
    ctx.store.record_approval(s7["run_date"], digest, "approved",
                              "rule change applied by operator")
    print("applied; active rule version is now %d" % version)


def cmd_rollback_rules(args):
    ctx = build_ctx(args)
    if not args.yes:
        if input("Roll back rules to v%d? [y/N] "
                 % args.to).strip().lower() != "y":
            print("not rolled back")
            return
    version = ctx.store.rollback_rules(args.to, reason=args.reason or "")
    print("rolled back; active rule version is now %d (params of v%d)"
          % (version, args.to))


def cmd_status(args):
    ctx = build_ctx(args)
    print("cash: %.2f" % ctx.store.get_cash())
    print("positions:", json.dumps(ctx.store.get_positions(), indent=2))
    print("rule history:")
    for h in ctx.store.rule_history():
        print("  v%d [%s] %s" % (h["version"], h["status"], h["reason"]))
    rows = ctx.store.conn.execute(
        "SELECT run_date, stage, status, attempts FROM stage_runs"
        " ORDER BY run_date, stage").fetchall()
    print("stage runs: %d" % len(rows))
    for r in rows[-20:]:
        print("  %s %-2s %-7s attempts=%d"
              % (r["run_date"], r["stage"], r["status"], r["attempts"]))


def cmd_export(args):
    ctx = build_ctx(args)
    os.makedirs(args.out, exist_ok=True)
    n = 0
    for stage in controller.STAGES + ["S7"]:
        art = ctx.store.stage_artifact(args.day, stage)
        if art is None:
            continue
        path = os.path.join(args.out, "%s_%s.json" % (args.day, stage))
        with open(path, "w") as fh:
            json.dump(art, fh, indent=2, sort_keys=True)
        n += 1
    print("exported %d artifacts for %s to %s" % (n, args.day, args.out))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--cache", default=datafeed.CACHE_DIR)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p = sub.add_parser("refresh")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--symbols", nargs="+")

    p = sub.add_parser("import-positions")
    p.add_argument("--file", required=True,
                   help="broker positions CSV: rows of symbol,qty and an"
                        " optional CASH,amount row")
    p.add_argument("--day", required=True)
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("run-day")
    p.add_argument("date")
    p.add_argument("--auto-approve", action="store_true")
    p.add_argument("--crash-after")
    p.add_argument("--crash-on")

    p = sub.add_parser("replay")
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--auto-approve", action="store_true")
    p.add_argument("--with-rules-eval", action="store_true")
    p.add_argument("--crash-after")
    p.add_argument("--crash-on")

    p = sub.add_parser("rules-eval")
    p.add_argument("--month", required=True)

    p = sub.add_parser("apply-rules")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("rollback-rules")
    p.add_argument("--to", type=int, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("approve")
    p.add_argument("--day", required=True)
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note", default="")

    p = sub.add_parser("report")
    p.add_argument("--day")
    p.add_argument("--month")
    p.add_argument("--out")

    sub.add_parser("status")

    p = sub.add_parser("export")
    p.add_argument("--day", required=True)
    p.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s")
    globals()["cmd_" + args.cmd.replace("-", "_")](args)


if __name__ == "__main__":
    main()
