"""SQLite persistent state for the workflow: positions, cash, trades,
approvals, stage checkpoints, artifacts, and rule versions.

Everything the long-running workflow needs to survive an interruption
lives in this one database. The controller consults ``stage_runs`` before
executing any stage, which is what makes resume idempotent: a stage that
completed for a given run_date is never re-executed.
"""

import hashlib
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_runs (
    run_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    artifact_hash TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_date, stage)
);
CREATE TABLE IF NOT EXISTS artifacts (
    hash TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    payload TEXT NOT NULL            -- JSON
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    decision TEXT NOT NULL,          -- approved | rejected
    note TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,              -- BUY | SELL
    qty REAL NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    approval_id INTEGER NOT NULL REFERENCES approvals(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_versions (
    version INTEGER PRIMARY KEY,
    params TEXT NOT NULL,            -- JSON
    reason TEXT NOT NULL,
    status TEXT NOT NULL,            -- active | superseded
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_curve (
    run_date TEXT PRIMARY KEY,
    total_value REAL NOT NULL
);
"""


class StateStore:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- bootstrap -----------------------------------------------------

    def is_initialized(self):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM portfolio").fetchone()
        return row["n"] > 0

    def initialize(self, cash, positions, rules, reason="initial rules v1"):
        """Seed portfolio state and rule version 1. Refuses to run twice."""
        if self.is_initialized():
            raise RuntimeError("state already initialized")
        self.conn.execute("INSERT INTO portfolio (id, cash) VALUES (1, ?)",
                          (float(cash),))
        for symbol, qty in positions.items():
            self.conn.execute(
                "INSERT INTO positions (symbol, qty) VALUES (?, ?)",
                (symbol, float(qty)))
        self.conn.execute(
            "INSERT INTO rule_versions (version, params, reason, status,"
            " created_at) VALUES (1, ?, ?, 'active', ?)",
            (json.dumps(rules, sort_keys=True), reason, time.time()))
        self.conn.commit()

    # ---- portfolio ------------------------------------------------------

    def get_cash(self):
        return self.conn.execute(
            "SELECT cash FROM portfolio WHERE id = 1").fetchone()["cash"]

    def get_positions(self):
        rows = self.conn.execute("SELECT symbol, qty FROM positions").fetchall()
        return {r["symbol"]: r["qty"] for r in rows}

    def apply_fill(self, run_date, symbol, side, qty, price, approval_id):
        """Apply one sandbox fill atomically: trade row, position, cash."""
        notional = qty * price
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO trades (run_date, symbol, side, qty, price,"
                " notional, approval_id, created_at) VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_date, symbol, side, qty, price, notional,
                 approval_id, time.time()))
            cash = self.get_cash()
            cash += -notional if side == "BUY" else notional
            self.conn.execute("UPDATE portfolio SET cash = ? WHERE id = 1",
                              (cash,))
            pos = self.get_positions().get(symbol, 0.0)
            pos += qty if side == "BUY" else -qty
            if abs(pos) < 1e-9:
                self.conn.execute("DELETE FROM positions WHERE symbol = ?",
                                  (symbol,))
            else:
                self.conn.execute(
                    "INSERT INTO positions (symbol, qty) VALUES (?, ?)"
                    " ON CONFLICT(symbol) DO UPDATE SET qty = excluded.qty",
                    (symbol, pos))
        return cur.lastrowid

    def get_trades(self, run_date=None):
        sql = "SELECT * FROM trades"
        args = ()
        if run_date:
            sql += " WHERE run_date = ?"
            args = (run_date,)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ---- checkpoints ----------------------------------------------------

    def stage_status(self, run_date, stage):
        row = self.conn.execute(
            "SELECT status FROM stage_runs WHERE run_date = ? AND stage = ?",
            (run_date, stage)).fetchone()
        return row["status"] if row else None

    def begin_stage(self, run_date, stage):
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage_runs (run_date, stage, status, attempts,"
                " updated_at) VALUES (?, ?, 'running', 1, ?)"
                " ON CONFLICT(run_date, stage) DO UPDATE SET"
                " status = 'running', attempts = attempts + 1,"
                " updated_at = excluded.updated_at",
                (run_date, stage, time.time()))

    def complete_stage(self, run_date, stage, artifact_hash=None):
        with self.conn:
            self.conn.execute(
                "UPDATE stage_runs SET status = 'done', artifact_hash = ?,"
                " updated_at = ? WHERE run_date = ? AND stage = ?",
                (artifact_hash, time.time(), run_date, stage))

    def fail_stage(self, run_date, stage):
        with self.conn:
            self.conn.execute(
                "UPDATE stage_runs SET status = 'failed', updated_at = ?"
                " WHERE run_date = ? AND stage = ?",
                (time.time(), run_date, stage))

    def attempts(self, run_date, stage):
        row = self.conn.execute(
            "SELECT attempts FROM stage_runs WHERE run_date = ? AND stage = ?",
            (run_date, stage)).fetchone()
        return row["attempts"] if row else 0

    # ---- artifacts (content-addressed) ----------------------------------

    def save_artifact(self, run_date, stage, payload):
        blob = json.dumps(payload, sort_keys=True, indent=2)
        digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO artifacts (hash, run_date, stage,"
                " payload) VALUES (?, ?, ?, ?)",
                (digest, run_date, stage, blob))
        return digest

    def get_artifact(self, digest):
        row = self.conn.execute(
            "SELECT payload FROM artifacts WHERE hash = ?",
            (digest,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def stage_artifact(self, run_date, stage):
        row = self.conn.execute(
            "SELECT artifact_hash FROM stage_runs WHERE run_date = ?"
            " AND stage = ?", (run_date, stage)).fetchone()
        if not row or not row["artifact_hash"]:
            return None
        return self.get_artifact(row["artifact_hash"])

    # ---- approvals -------------------------------------------------------

    def record_approval(self, run_date, proposal_hash, decision, note=""):
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO approvals (run_date, proposal_hash, decision,"
                " note, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_date, proposal_hash, decision, note, time.time()))
        return cur.lastrowid

    def find_approval(self, run_date, proposal_hash, decision="approved"):
        row = self.conn.execute(
            "SELECT id FROM approvals WHERE run_date = ? AND proposal_hash = ?"
            " AND decision = ? ORDER BY id DESC LIMIT 1",
            (run_date, proposal_hash, decision)).fetchone()
        return row["id"] if row else None

    # ---- rule versions ----------------------------------------------------

    def active_rules(self):
        row = self.conn.execute(
            "SELECT version, params FROM rule_versions WHERE status = 'active'"
            " ORDER BY version DESC LIMIT 1").fetchone()
        return row["version"], json.loads(row["params"])

    def add_rule_version(self, params, reason):
        with self.conn:
            self.conn.execute(
                "UPDATE rule_versions SET status = 'superseded'"
                " WHERE status = 'active'")
            nxt = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v"
                " FROM rule_versions").fetchone()["v"]
            self.conn.execute(
                "INSERT INTO rule_versions (version, params, reason, status,"
                " created_at) VALUES (?, ?, ?, 'active', ?)",
                (nxt, json.dumps(params, sort_keys=True), reason,
                 time.time()))
        return nxt

    def rollback_rules(self, to_version, reason=""):
        """Re-activate an earlier rule version (records a new version row
        whose params equal the target, keeping the audit trail append-only)."""
        row = self.conn.execute(
            "SELECT params FROM rule_versions WHERE version = ?",
            (to_version,)).fetchone()
        if not row:
            raise KeyError("no rule version %s" % to_version)
        return self.add_rule_version(
            json.loads(row["params"]),
            reason or "rollback to v%s" % to_version)

    def rule_history(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT version, reason, status, created_at FROM rule_versions"
            " ORDER BY version").fetchall()]

    # ---- equity curve ------------------------------------------------------

    def record_equity(self, run_date, total_value):
        with self.conn:
            self.conn.execute(
                "INSERT INTO equity_curve (run_date, total_value) VALUES"
                " (?, ?) ON CONFLICT(run_date) DO UPDATE SET"
                " total_value = excluded.total_value",
                (run_date, float(total_value)))

    def equity_history(self):
        return [(r["run_date"], r["total_value"]) for r in self.conn.execute(
            "SELECT run_date, total_value FROM equity_curve"
            " ORDER BY run_date").fetchall()]
