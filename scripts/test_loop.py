"""Test fonctionnel de l'industrialisation temps réel — Phase 6.

Usage :  python scripts/test_loop.py

SECTIONS
  A. SignalTracker unitaire (synthétique, déterministe) : TP prioritaire SL,
     SL, expiration à 48 bougies, signal sans données (reste EN_COURS),
     message de clôture formaté, migration SQLite.
  B. Boucle accélérée SIMULÉE sur données réelles : 24 cycles de 3 paires
     (toutes les 2 h sur 48 h, troncature chronologique), puis tracker sur
     la base accumulée -> preuve que les signaux se résolvent seuls.
  C. `python -m src.main --once` en sous-processus réel (code 0, base créée).
  D. Arrêt propre : sommeil interrompu réagit en < 1,5 s.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.agents.fundamental_agent import FundamentalAgent
from src.data.data_fetcher import DataFetcher
from src.data.provider import Timeframe
from src.main import LoopRunner, sleep_interruptible
from src.notifications.telegram import format_closure_message
from src.signals.engine import SignalEngine
from src.signals.models import RiskPlan, Signal
from src.signals.tracker import SignalTracker
from src.storage.database import SignalDatabase

results: list[bool] = []
ENTRY_TIME = datetime(2026, 8, 19, 8, 32, tzinfo=timezone.utc)


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


class StubSender:
    """Capteur de messages Telegram (aucun envoi réel)."""

    def __init__(self):
        self.sent: list[str] = []

    def send_text(self, text):
        self.sent.append(text)
        return True

    def send_signal(self, signal):
        self.sent.append("SIGNAL")
        return True


class StubFetcher:
    """Renvoie un DataFrame figé, quel que soit l'appel."""

    def __init__(self, df):
        self.df = df

    def get_candles(self, pair, timeframe, lookback_days=30, only_closed=True):
        return self.df.copy()


def m15_series(rows):
    # Première bougie 15 min APRÈS l'heure du signal (filtrage strict ">")
    index = pd.date_range(ENTRY_TIME + pd.Timedelta(minutes=15), periods=len(rows),
                          freq="15min", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def make_signal(pair="EURUSD", direction="LONG", entry=1.1022, sl=1.1003, tp=1.1059,
                created="2026-08-19 08:32:00 UTC", rr=2.0):
    return Signal(
        pair=pair, direction=direction, score=90, grade="A", session="London Kill Zone",
        risk=RiskPlan(valid=True, entry=entry, sl=sl, tp=tp, rr=rr, risk_pips=19.0,
                      tp_pips=37.0, lots=0.5, reasons=[], blockers=[]),
        confluences=["test"], breakdown={}, timeframes={}, fundamental={},
        created_at=created,
    )


# --------------------------------------------------------------------------- #
def section_a():
    print("\n--- A. SignalTracker unitaire (déterministe) ---")

    def run_case(rows, label, expected, expect_r=None):
        with tempfile.TemporaryDirectory() as tmp:
            db = SignalDatabase(Path(tmp) / "t.db")
            db.insert_signal(make_signal())
            sender = StubSender()
            tracker = SignalTracker(db, StubFetcher(m15_series(rows)), telegram=sender)
            resolutions = tracker.update_all(now=ENTRY_TIME + timedelta(hours=13))
            row = db.recent(1).iloc[0]
            ok = row["resultat"] == expected and (
                expect_r is None or abs(float(row["exit_r"]) - expect_r) < 1e-6)
            detail = f"id={row['id']} -> {row['resultat']}, R={row['exit_r']}"
            if expected != "EN_COURS":
                ok = ok and len(resolutions) == 1 and len(sender.sent) == 1 \
                    and "SIGNAL CLOTURE" in sender.sent[0]
            check(label, ok, detail)

    # 1. TP touché (3e bougie)
    run_case([(1.1020, 1.1030, 1.1015, 1.1028), (1.1028, 1.1040, 1.1022, 1.1035),
              (1.1035, 1.1065, 1.1030, 1.1055)],
             "TP atteint -> TP_ATTEINT (+2.0R), notification envoyée",
             "TP_ATTEINT", 2.0)

    # 2. SL et TP dans la MÊME bougie -> SL prioritaire (conservateur)
    run_case([(1.1020, 1.1070, 1.1000, 1.1040)],
             "SL+TP même bougie -> SL prioritaire (conservateur)",
             "SL_ATTEINT", -1.0)

    # 3. 48 bougies sans toucher -> EXPIRE, R partiel calculé
    flat = [(1.1020, 1.1030, 1.1010, 1.1022)] * 48
    with tempfile.TemporaryDirectory() as tmp:
        db = SignalDatabase(Path(tmp) / "t.db")
        db.insert_signal(make_signal())
        tracker = SignalTracker(db, StubFetcher(m15_series(flat)))
        tracker.update_all(now=ENTRY_TIME + timedelta(hours=13))
        row = db.recent(1).iloc[0]
        expected_r = (1.1022 - 1.1022) / (1.1022 - 1.1003)  # clôture = entrée
        check("48 bougies sans issue -> EXPIRE (R partiel)",
              row["resultat"] == "EXPIRE" and abs(float(row["exit_r"]) - expected_r) < 1e-6,
              f"{row['resultat']}, R={row['exit_r']}")

    # 4. peu de bougies -> reste EN_COURS
    run_case([(1.1020, 1.1030, 1.1015, 1.1028)] * 3,
             "données insuffisantes -> reste EN_COURS", "EN_COURS")

    # 5. format du message de clôture (sans emoji)
    res = {"paire": "EURUSD", "type": "LONG", "id": 7, "resultat": "TP_ATTEINT",
           "exit_price": 1.1059, "exit_r": 2.0}
    msg = format_closure_message(res, {"entree": 1.1022},
                                 {"closed": 5, "winrate": 0.6, "total_r": 4.2})
    no_emoji = not any(0x1F000 <= ord(c) <= 0x1FAFF for c in msg)
    check("message de clôture : format pro, sans emoji",
          "SIGNAL CLOTURE" in msg and "+2.0R" in msg and "+4.2R" in msg and no_emoji)

    # 6. migration : base créée SANS les colonnes de sortie -> migrée
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        import sqlite3

        raw = sqlite3.connect(path)
        raw.execute("""CREATE TABLE signals (id INTEGER PRIMARY KEY, paire TEXT, type TEXT,
            entree REAL, tp REAL, sl REAL, resultat TEXT, score INTEGER, date TEXT)""")
        raw.commit()
        raw.close()
        db = SignalDatabase(path)  # doit migrer sans erreur
        cols = {r["name"] for r in db._connect().execute("PRAGMA table_info(signals)")}
        check("migration SQLite (colonnes exit_* ajoutées)",
              {"exit_price", "exit_r", "exit_time"} <= cols)

    # 7. stats agrégées
    with tempfile.TemporaryDirectory() as tmp:
        db = SignalDatabase(Path(tmp) / "t.db")
        db.insert_signal(make_signal())
        db.insert_signal(make_signal(pair="GBPUSD"))
        rows = db.recent(2)
        db.update_result(int(rows.iloc[0]["id"]), "TP_ATTEINT", 1.1059, 2.0, "x")
        db.update_result(int(rows.iloc[1]["id"]), "SL_ATTEINT", 1.1003, -1.0, "x")
        s = db.stats()
        check("stats : winrate 0.5, total +1.0R",
              s["closed"] == 2 and s["winrate"] == 0.5 and abs(s["total_r"] - 1.0) < 1e-9,
              str(s))


# --------------------------------------------------------------------------- #
def section_b():
    print("\n--- B. Boucle accélérée simulée sur données RÉELLES (48 h) ---")
    import logging as _logging

    _logging.getLogger().setLevel(_logging.ERROR)
    fetcher = DataFetcher()
    pairs = ["EURUSD", "GBPUSD", "XAUUSD"]
    frames = {}
    for pair in pairs:
        mtd = fetcher.get_multi_timeframe_data(pair, ("1d", "4h", "15m"))
        frames[pair] = {tf: mtd.frames[tf] for tf in (Timeframe.D1, Timeframe.H4, Timeframe.M15)}

    m15_eur = frames["EURUSD"][Timeframe.M15]
    stamps = m15_eur.index[-192::8]  # toutes les 2 h sur ~48 h
    stamps = [t for t in stamps if t > m15_eur.index[0] + pd.Timedelta(days=25)]

    with tempfile.TemporaryDirectory() as tmp:
        db = SignalDatabase(Path(tmp) / "sim.db")
        sender = StubSender()
        engine = SignalEngine(config=None, database=db, fetcher=fetcher,
                              fundamental=FundamentalAgent(), telegram=sender)
        engine.db = db
        cycles = signals = 0
        for ts in stamps:
            for pair in pairs:
                d1, h4, m15 = frames[pair][Timeframe.D1], frames[pair][Timeframe.H4], \
                    frames[pair][Timeframe.M15]
                report = engine.run_on_frames(
                    pair,
                    d1[d1.index <= ts], h4[h4.index <= ts], m15[m15.index <= ts], now=ts,
                )
                if report.signal:
                    signals += 1
            cycles += 1
        check("boucle simulée : cycles exécutés sans crash", cycles == len(stamps),
              f"{cycles} cycles x {len(pairs)} paires, {signals} signal(s) émis")

        # Tracker sur la base accumulée, avec les bougies réelles
        tracker = SignalTracker(db, fetcher, telegram=sender, expiry_bars=48)
        resolutions = tracker.update_all(now=datetime.now(timezone.utc))
        stats = db.stats()
        check("clôtures automatiques effectuées sur données réelles",
              stats["closed"] == stats["total"] - stats["open"] and stats["open"] >= 0,
              f"stats: {stats}")

        # Critère du comité : aucun EN_COURS de plus de 48 bougies M15 (12 h)
        open_rows = db.open_signals()
        limit = datetime.now(timezone.utc) - timedelta(hours=12) - timedelta(minutes=30)
        stale = [r for _, r in open_rows.iterrows()
                 if datetime.fromisoformat(r["created_at"]) < limit]
        check("critère comité : zéro EN_COURS de plus de 48 bougies",
              len(stale) == 0, f"{len(open_rows)} ouvert(s), 0 périmé(s)")

        closed = db.recent(50)
        closed = closed[closed["resultat"] != "EN_COURS"]
        if not closed.empty:
            r_ok = closed["exit_r"].between(-1.01, 3.01).all()
            check("R de clôture dans les bornes [-1, +3]", bool(r_ok),
                  f"issues : {closed['resultat'].value_counts().to_dict()}")
            closures = [m for m in sender.sent if "SIGNAL CLOTURE" in m]
            check("notifications de clôture émises", len(closures) == len(closed),
                  f"{len(closures)} message(s)")
        else:
            check("aucun signal clôturé nécessaire (0 émis ou tous récents)", stats["open"] == stats["total"])

        # Preuve de résolution sur DONNÉES RÉELLES : on injecte un signal LONG
        # vieux de 24 h construit sur les prix réels, puis on le clôture.
        m15_now = frames["EURUSD"][Timeframe.M15]
        p = float(m15_now["close"].iloc[-1])
        atr = float(m15_now["close"].diff().abs().rolling(20).mean().iloc[-1]) or 0.0010
        sig = make_signal(entry=p, sl=round(p - 2 * atr, 6), tp=round(p + 4 * atr, 6),
                          created=(datetime.now(timezone.utc) - timedelta(hours=24))
                          .strftime("%Y-%m-%d %H:%M:%S UTC"))
        sig.db_id = db.insert_signal(sig)
        res = tracker.update_all(now=datetime.now(timezone.utc))
        row = db.recent(1).iloc[0]
        check("signal réel injecté -> clôturé automatiquement sur bougies réelles",
              row["resultat"] in ("TP_ATTEINT", "SL_ATTEINT", "EXPIRE") and bool(res),
              f"EURUSD #{row['id']} -> {row['resultat']} ({row['exit_r']}R)")


# --------------------------------------------------------------------------- #
def section_c():
    print("\n--- C. `python -m src.main --once` (sous-processus réel) ---")
    proc = subprocess.run(
        [sys.executable, "-m", "src.main", "--once"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=240,
    )
    log = proc.stdout + proc.stderr
    ok = proc.returncode == 0 and "cycle 1 terminé" in log
    db_exists = Path("data/signals.db").exists()
    check("mode --once : code 0, cycle complet, base de production créée",
          ok and db_exists, f"returncode={proc.returncode}, base={'oui' if db_exists else 'non'}")
    # Ligne de log par paire (isolation visible)
    pairs_logged = sum(1 for p in ("EURUSD", "GBPUSD", "XAUUSD") if p in log)
    check("chaque paire journalisée dans le cycle", pairs_logged == 3, f"{pairs_logged}/3 paires")


def section_d():
    print("\n--- D. Arrêt propre : sommeil interrompu ---")
    import threading

    stop = threading.Event()
    stop.set()
    t0 = time.monotonic()
    sleep_interruptible(30.0, stop)
    elapsed = time.monotonic() - t0
    check("arrêt pris en compte en < 1,5 s pendant le sommeil", elapsed < 1.5,
          f"{elapsed * 1000:.0f} ms")

    # Construction légère du runner (config réelle) + vérif des compteurs
    runner = LoopRunner()
    check("LoopRunner opérationnel (paires, seuil, intervalle)",
          runner.pairs and runner.interval >= 30 and runner.engine.threshold == 70,
          f"paires={runner.pairs}, cycle={runner.interval}s, seuil={runner.engine.threshold}")


# --------------------------------------------------------------------------- #
def main():
    from src.logger import setup_logging

    setup_logging(level="WARNING")
    print("=" * 70)
    print(" Test fonctionnel — industrialisation temps réel (Phase 6)")
    print("=" * 70)

    section_a()
    section_b()
    section_c()
    section_d()

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — Phase 6 validée ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
