"""Workflow controller: scheduling, dependency order, retries, idempotent
resume, and the approval gate.

The controller owns execution control; the stages own domain logic. Before
running a stage the controller checks the checkpoint table: a stage marked
'done' for this run_date is skipped untouched, which is exactly how an
interrupted run resumes without repeating completed actions.
"""

import logging
import time

from . import review, stages

log = logging.getLogger("pma.controller")

STAGES = ["S1", "S2", "S3", "S4", "S5", "S6"]

_STAGE_FNS = {
    "S1": lambda ctx, day: stages.s1_ingest(ctx, day),
    "S2": lambda ctx, day: stages.s2_monitor(ctx, day),
    "S3": lambda ctx, day: stages.s3_signal(ctx, day),
    "S4": lambda ctx, day: review.review(ctx, day),
    "S5": lambda ctx, day: stages.s5_approval(ctx, day,
                                              auto=ctx.auto_approve),
    "S6": lambda ctx, day: stages.s6_execute(ctx, day),
}


class SimulatedCrash(RuntimeError):
    """Raised by crash injection to prove checkpoint-based recovery."""


class PipelineError(RuntimeError):
    pass


class Context:
    def __init__(self, store, cfg, cache_dir=None, inbox_dir=None,
                 auto_approve=False, crash_after=None, crash_on=None):
        self.store = store
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.inbox_dir = inbox_dir
        self.auto_approve = auto_approve
        self.crash_after = crash_after    # stage id, e.g. "S2"
        self.crash_on = crash_on          # run_date to crash on (None = first)


def run_day(ctx, day):
    """Run S1..S6 for one trading day, resuming from checkpoints.

    Returns a dict of stage -> artifact hash for the stages that are done.
    """
    results = {}
    crashed = False
    for stage in STAGES:
        status = ctx.store.stage_status(day, stage)
        if status == "done":
            log.info("%s %s: checkpoint found, skipping", day, stage)
            art = ctx.store.stage_artifact(day, stage)
            results[stage] = art
            continue
        results[stage] = _run_with_retries(ctx, day, stage)
        if ctx.crash_after == stage and (
                ctx.crash_on is None or ctx.crash_on == day):
            crashed = True
            crash_stage = stage
            ctx.crash_after = None  # crash at most once per run
            log.error("%s: SIMULATED CRASH after %s", day, stage)
            break
    if crashed:
        raise SimulatedCrash("crash injected after %s on %s"
                             % (crash_stage, day))
    return results


def _run_with_retries(ctx, day, stage):
    max_retries = ctx.cfg["limits"]["max_retries"]
    timeout = ctx.cfg["limits"]["stage_timeout_sec"]
    for attempt in range(1, max_retries + 2):  # first try + retries
        ctx.store.begin_stage(day, stage)
        started = time.time()
        try:
            artifact = _STAGE_FNS[stage](ctx, day)
            if time.time() - started > timeout:
                raise PipelineError("%s exceeded stage timeout" % stage)
            digest = ctx.store.save_artifact(day, stage, artifact)
            ctx.store.complete_stage(day, stage, digest)
            log.info("%s %s: done (artifact %s)", day, stage, digest)
            return artifact
        except Exception as exc:  # noqa: BLE001 - controller boundary
            ctx.store.fail_stage(day, stage)
            log.warning("%s %s: attempt %d failed: %s",
                        day, stage, attempt, exc)
            if attempt > max_retries:
                raise PipelineError(
                    "%s on %s failed after %d retries: %s"
                    % (stage, day, max_retries, exc))
            time.sleep(min(2 ** attempt, 8))  # exponential backoff
