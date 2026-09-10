"""ShadowRecorder — persistance du paper shadow mode (base SQLite dédiée).

Deux tables :

    decisions        UNE LIGNE PAR CYCLE (SIGNAL ou NO_TRADE) — le journal
                     complet §33 : stage, code, détail, et le payload §32
                     quand c'est un SIGNAL ;
    signal_outcomes  clôture papier des signaux simulés + MAE/MFE (§28).

La base est `data/shadow.db`, distincte de la base live — le moteur live
n'est jamais lu ni écrit par ce module.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    decision TEXT NOT NULL,          -- SIGNAL / NO_TRADE
    stage TEXT, code TEXT, detail TEXT,
    regime TEXT, score INTEGER, grade TEXT,
    direction TEXT,
    entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
    rr1 REAL, rr2 REAL, rr3 REAL,
    zone_id TEXT, scenario TEXT,
    setup TEXT, why_now TEXT, invalidation TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_decisions_code ON decisions(code);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    decision_id INTEGER PRIMARY KEY,
    ts_entry TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL, sl REAL NOT NULL, tp1 REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / TP / SL / EXPIRE
    exit_price REAL, exit_time TEXT,
    mae_r REAL, mfe_r REAL,
    time_to_exit_min INTEGER,
    tp2 REAL, tp3 REAL,
    regime TEXT, scenario TEXT, zone_id TEXT, score INTEGER
);
"""


class ShadowDatabase:
    """Journal du shadow mode (écrit par le cycle, lu par le rapport)."""

    def __init__(self, path: str | Path = "data/shadow.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ #
    def record_decision(self, decision, now) -> int:
        """Enregistre une EngineDecision (SIGNAL ou NO_TRADE) ; renvoie l'id."""
        with self._connect() as db:
            cur = db.execute(
                """INSERT INTO decisions
                   (ts, decision, stage, code, detail, regime, score, grade,
                    direction, entry, sl, tp1, tp2, tp3, rr1, rr2, rr3,
                    zone_id, scenario, setup, why_now, invalidation)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(now), decision.decision, decision.stage, decision.code,
                 decision.detail[:400], decision.regime, decision.score,
                 decision.grade, decision.direction, decision.entry,
                 decision.sl, decision.tp1, decision.tp2, decision.tp3,
                 decision.rr1, decision.rr2, decision.rr3, decision.zone_id,
                 decision.scenario_name, decision.setup[:300],
                 decision.why_now[:300], decision.invalidation[:300]),
            )
            decision_id = int(cur.lastrowid)
            if decision.decision == "SIGNAL":
                db.execute(
                    """INSERT INTO signal_outcomes
                       (decision_id, ts_entry, direction, entry, sl, tp1,
                        status, tp2, tp3, regime, scenario, zone_id, score)
                       VALUES (?,?,?,?,?,?, 'OPEN', ?,?,?,?,?,?)""",
                    (decision_id, str(now), decision.direction, decision.entry,
                     decision.sl, decision.tp1, decision.tp2, decision.tp3,
                     decision.regime, decision.scenario_name, decision.zone_id,
                     decision.score),
                )
            return decision_id

    # ------------------------------------------------------------------ #
    def open_outcomes(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(db.execute(
                "SELECT * FROM signal_outcomes WHERE status = 'OPEN' "
                "ORDER BY decision_id"))

    def close_outcome(self, decision_id: int, status: str, exit_price: float,
                      exit_time, mae_r: float, mfe_r: float,
                      time_to_exit_min: int) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE signal_outcomes
                   SET status = ?, exit_price = ?, exit_time = ?,
                       mae_r = ?, mfe_r = ?, time_to_exit_min = ?
                   WHERE decision_id = ? AND status = 'OPEN'""",
                (status, round(exit_price, 6), str(exit_time),
                 round(mae_r, 3), round(mfe_r, 3), int(time_to_exit_min),
                decision_id),
            )

    # ------------------------------------------------------------------ #
    def history_records(self):
        """Historique des signaux papier au format SignalRecord (P9)."""
        from ..analysis.anti_overtrading_engine import SignalRecord
        import pandas as pd

        with self._connect() as db:
            rows = list(db.execute(
                "SELECT o.decision_id, o.direction, o.ts_entry, o.status, "
                "d.zone_id FROM signal_outcomes o "
                "JOIN decisions d ON d.id = o.decision_id "
                "ORDER BY o.decision_id"))
        out = []
        for r in rows:
            ts = pd.Timestamp(r["ts_entry"])
            exit_time = None
            exit_kind = None
            if r["status"] in ("TP", "SL", "EXPIRE"):
                with self._connect() as db:
                    o = db.execute("SELECT exit_time FROM signal_outcomes "
                                   "WHERE decision_id = ?",
                                   (r["decision_id"],)).fetchone()
                exit_time = pd.Timestamp(o["exit_time"])
                exit_kind = {"TP": "TP", "SL": "SL", "EXPIRE": "EXPIRE"}[r["status"]]
            out.append(SignalRecord(
                zone_id=r["zone_id"] or f"SH-{r['decision_id']}",
                direction=r["direction"], time=ts,
                exit_time=exit_time, exit_kind=exit_kind))
        return out

    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        """Métriques §27/§36 du moteur papier."""
        with self._connect() as db:
            n_dec = int(db.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"])
            n_sig = int(db.execute(
                "SELECT COUNT(*) c FROM decisions WHERE decision = 'SIGNAL'"
            ).fetchone()["c"])
            codes = dict(db.execute(
                "SELECT code, COUNT(*) c FROM decisions "
                "WHERE decision = 'NO_TRADE' GROUP BY code ORDER BY c DESC"
            ).fetchall()) if n_dec else {}
            rows = list(db.execute(
                """SELECT status, exit_price, entry, sl, mae_r, mfe_r,
                          time_to_exit_min, regime, scenario, direction
                   FROM signal_outcomes WHERE status != 'OPEN'"""))
        closed = [r for r in rows]
        tp = [r for r in closed if r["status"] == "TP"]
        sl = [r for r in closed if r["status"] == "SL"]
        exp = [r for r in closed if r["status"] == "EXPIRE"]

        def r_of(row):
            risk = abs(row["entry"] - row["sl"]) or 1e-9
            if row["direction"] == "LONG":
                return (row["exit_price"] - row["entry"]) / risk
            return (row["entry"] - row["exit_price"]) / risk

        rs = [r_of(r) for r in closed]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        # losing streak max
        streak = max_streak = 0
        for r in closed:
            if r_of(r) <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return {
            "decisions": n_dec, "signals": n_sig,
            "no_trade_codes": codes,
            "closed": len(closed), "tp": len(tp), "sl": len(sl),
            "expire": len(exp),
            "winrate": round(len(wins) / len(closed), 3) if closed else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "total_r": round(sum(rs), 2) if rs else 0.0,
            "profit_factor": round(sum(wins) / -sum(losses), 2)
            if losses and sum(losses) < 0 else None,
            "max_losing_streak": max_streak,
            "avg_mae_r": round(sum(r["mae_r"] for r in closed) / len(closed), 2)
            if closed else None,
            "avg_mfe_r": round(sum(r["mfe_r"] for r in closed) / len(closed), 2)
            if closed else None,
            "avg_time_to_exit_min": round(
                sum(r["time_to_exit_min"] for r in closed) / len(closed))
            if closed else None,
        }
