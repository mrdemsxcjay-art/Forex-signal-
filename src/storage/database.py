"""Persistance SQLite — historique des signaux + issues (TP/SL/EXPIRE).

Schéma de la spécification (colonnes d'origine conservées) :
    id, paire, type, entree, tp, sl, resultat, score, date
Extensions (compatible, ajoutées par migration automatique si besoin) :
    session, grade, rr, tp_pips, sl_pips, lots, confluences (JSON),
    breakdown (JSON), timeframes (JSON), fundamental (JSON), created_at,
    exit_price, exit_r, exit_time       <- remplis par le SignalTracker

`resultat` : 'EN_COURS' à l'émission, puis 'TP_ATTEINT' / 'SL_ATTEINT' /
'EXPIRE' (délai de 48 bougies M15 = 12 h) — engagement du comité de pilotage :
aucun signal ne reste EN_COURS plus de 48 bougies.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..signals.models import Signal

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paire TEXT NOT NULL,
    type TEXT NOT NULL,
    entree REAL,
    tp REAL,
    sl REAL,
    resultat TEXT,
    score INTEGER,
    date TEXT,
    session TEXT,
    grade TEXT,
    rr REAL,
    tp_pips REAL,
    sl_pips REAL,
    lots REAL,
    confluences TEXT,
    breakdown TEXT,
    timeframes TEXT,
    fundamental TEXT,
    created_at TEXT,
    exit_price REAL,
    exit_r REAL,
    exit_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_paire_date ON signals(paire, date);
CREATE INDEX IF NOT EXISTS idx_signals_resultat ON signals(resultat);
"""

#: Colonnes ajoutées aux bases créées avant la phase 6 (migration transparente).
_MIGRATION_COLUMNS = {
    "exit_price": "REAL",
    "exit_r": "REAL",
    "exit_time": "TEXT",
}


def _parse_created(value: str) -> datetime:
    """'2026-08-19 08:32:00 UTC' ou ISO -> datetime UTC."""
    value = str(value).strip()
    try:
        if value.endswith("UTC"):
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)


class SignalDatabase:
    """Base SQLite locale (fichier unique, gratuite, sans serveur)."""

    def __init__(self, path: str | Path = "data/signals.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            self._migrate(db)
        logger.debug("Base SQLite prête : %s", self.path)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Ajoute les colonnes manquantes sur une base antérieure (idempotent)."""
        existing = {row["name"] for row in db.execute("PRAGMA table_info(signals)")}
        for column, sql_type in _MIGRATION_COLUMNS.items():
            if column not in existing:
                db.execute(f"ALTER TABLE signals ADD COLUMN {column} {sql_type}")
                logger.info("Migration SQLite : colonne %s ajoutée", column)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ #
    #  Écritures
    # ------------------------------------------------------------------ #
    def insert_signal(self, signal: Signal) -> int:
        """Enregistre un signal et renvoie son id."""
        try:  # horodatage d'analyse (déterministe) -> ISO aware pour le cooldown
            created = _parse_created(signal.created_at).astimezone(timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            created = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            cur = db.execute(
                """INSERT INTO signals
                   (paire, type, entree, tp, sl, resultat, score, date,
                    session, grade, rr, tp_pips, sl_pips, lots,
                    confluences, breakdown, timeframes, fundamental, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal.pair, signal.direction,
                    signal.risk.entry, signal.risk.tp, signal.risk.sl,
                    "EN_COURS", signal.score, signal.created_at,
                    signal.session, signal.grade, signal.risk.rr,
                    signal.risk.tp_pips, signal.risk.risk_pips, signal.risk.lots,
                    json.dumps(signal.confluences, ensure_ascii=False),
                    json.dumps(signal.breakdown, ensure_ascii=False),
                    json.dumps(signal.timeframes, ensure_ascii=False),
                    json.dumps(signal.fundamental, ensure_ascii=False),
                    created,
                ),
            )
            return int(cur.lastrowid)

    def update_result(
        self,
        signal_id: int,
        resultat: str,
        exit_price: float | None = None,
        exit_r: float | None = None,
        exit_time: str | None = None,
    ) -> None:
        """Clôture un signal : 'TP_ATTEINT' / 'SL_ATTEINT' / 'EXPIRE'."""
        with self._connect() as db:
            db.execute(
                """UPDATE signals
                   SET resultat = ?, exit_price = ?, exit_r = ?, exit_time = ?
                   WHERE id = ?""",
                (resultat, exit_price, exit_r, exit_time, signal_id),
            )

    # ------------------------------------------------------------------ #
    #  Lectures
    # ------------------------------------------------------------------ #
    def open_signals(self) -> pd.DataFrame:
        """Signaux non résolus (alimentation du SignalTracker)."""
        with self._connect() as db:
            return pd.read_sql_query(
                "SELECT * FROM signals WHERE resultat = 'EN_COURS' ORDER BY id", db
            )

    def count_today(self, pair: str, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE paire = ? AND date(date) = date(?)",
                (pair.upper(), now.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchone()
            return int(row["n"])

    def last_signal_time(self, pair: str) -> datetime | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT created_at FROM signals WHERE paire = ? ORDER BY id DESC LIMIT 1",
                (pair.upper(),),
            ).fetchone()
        return _parse_created(row["created_at"]) if row and row["created_at"] else None

    def recent(self, limit: int = 50) -> pd.DataFrame:
        with self._connect() as db:
            return pd.read_sql_query(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", db, params=(limit,)
            )

    def stats(self) -> dict:
        """Statistiques des signaux clôturés (winrate, R cumulé...)."""
        with self._connect() as db:
            row = db.execute(
                """SELECT
                     COUNT(*) AS total,
                     SUM(CASE WHEN resultat = 'EN_COURS' THEN 1 ELSE 0 END) AS open_count,
                     SUM(CASE WHEN resultat = 'TP_ATTEINT' THEN 1 ELSE 0 END) AS tp,
                     SUM(CASE WHEN resultat = 'SL_ATTEINT' THEN 1 ELSE 0 END) AS sl,
                     SUM(CASE WHEN resultat = 'EXPIRE'     THEN 1 ELSE 0 END) AS expired,
                     AVG(exit_r) AS avg_r,
                     SUM(exit_r) AS total_r
                   FROM signals"""
            ).fetchone()
        total = int(row["total"] or 0)
        closed = total - int(row["open_count"] or 0)
        wins = int(row["tp"] or 0)
        return {
            "total": total,
            "open": int(row["open_count"] or 0),
            "closed": closed,
            "tp": wins,
            "sl": int(row["sl"] or 0),
            "expired": int(row["expired"] or 0),
            "winrate": round(wins / closed, 3) if closed else None,
            "avg_r": round(float(row["avg_r"]), 3) if row["avg_r"] is not None else None,
            "total_r": round(float(row["total_r"]), 2) if row["total_r"] is not None else 0.0,
        }
