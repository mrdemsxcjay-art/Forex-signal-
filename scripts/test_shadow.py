"""Tests du Paper Shadow Mode — PHASE 11 (synthétique + local, hors-ligne)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.adaptive_engine import EngineDecision
from src.shadow.shadow_recorder import ShadowDatabase
from src.shadow.shadow_tracker import ShadowTracker

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def sig_decision(entry=1.1000, sl=1.0970, tp1=1.1090, direction="LONG"):
    return EngineDecision(
        decision="SIGNAL", stage="OK", code="SETUP_VALID", detail="test",
        direction=direction, regime="RANGE", entry=entry, sl=sl, tp1=tp1,
        tp2=None, tp3=None, rr1=3.0, rr2=None, rr3=None,
        setup="thèse test", market_structure="alignée", liquidity_narrative="x",
        confirmation="engulfing", why_now="sweep + confirmation",
        invalidation="sweep", score=78, grade="A", zone_id="OB-T1",
        scenario_name="B",
    )


def no_trade_decision(code="REGIME_UNCERTAIN"):
    return EngineDecision(decision="NO_TRADE", stage="REGIME", code=code,
                          detail="régime TRANSITION : aucune majorité structurelle")


class StubFetcher:
    def __init__(self, df):
        self.df = df

    def get_candles(self, pair, timeframe, lookback_days=30, only_closed=True):
        return self.df.copy()


def m15_bars(bars, start="2026-06-11 08:00"):
    idx = pd.date_range(start, periods=len(bars), freq="15min", tz="UTC")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def main() -> int:
    print("=" * 70)
    print(" Tests Paper Shadow Mode — PHASE 11")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp:
        db = ShadowDatabase(Path(tmp) / "shadow.db")
        NOW = pd.Timestamp("2026-06-11 12:00", tz="UTC")

        # 1. Journal §33 : NO_TRADE enregistré avec son code
        db.record_decision(no_trade_decision(), NOW - pd.Timedelta(hours=1))
        with db._connect() as c:
            rows = list(c.execute("SELECT decision, code FROM decisions"))
        check("NO_TRADE journalisé avec code §33",
              len(rows) == 1 and rows[0]["decision"] == "NO_TRADE"
              and rows[0]["code"] == "REGIME_UNCERTAIN")

        # 2. SIGNAL papier -> outcome OPEN créé
        did = db.record_decision(sig_decision(), NOW)
        outcomes = db.open_outcomes()
        check("SIGNAL papier -> outcome OPEN",
              len(outcomes) == 1 and outcomes[0]["decision_id"] == did
              and outcomes[0]["status"] == "OPEN")

        # 3. Clôture TP avec MAE/MFE exacts
        #    risque = 30 pips ; bougies : adverse -15 pips (MAE -0.5R),
        #    favorable jusqu'à TP (+90 pips = 3R) à la 4e bougie
        bars = m15_bars([
            (1.1000, 1.1005, 1.0985, 1.0995),   # MAE -0.5R (12:15)
            (1.0995, 1.1010, 1.0992, 1.1008),
            (1.1008, 1.1030, 1.1005, 1.1028),
            (1.1028, 1.1095, 1.1025, 1.1090),   # TP touché (13:00)
        ], start="2026-06-11 12:15")
        tracker = ShadowTracker(db, StubFetcher(bars))
        res = tracker.update_all(NOW + pd.Timedelta(hours=2))
        with db._connect() as c:
            o = c.execute("SELECT * FROM signal_outcomes WHERE decision_id = ?",
                          (did,)).fetchone()
        # MFE >= 3R : la bougie TP a un high (1.1095) qui DÉPASSE l'objectif
        # (1.1090) -> l'excursion réelle vaut 3.17R, pas exactement 3.0R.
        check("clôture TP avec MAE -0.5R et MFE >= 3R mesurés",
              o["status"] == "TP" and abs(o["mae_r"] - (-0.5)) < 0.01
              and 3.0 <= o["mfe_r"] < 3.3 and o["time_to_exit_min"] == 60,
              f"status {o['status']} mae {o['mae_r']} mfe {o['mfe_r']} "
              f"en {o['time_to_exit_min']} min")

        # 4. Idempotence : re-run ne re-clôture pas
        res2 = tracker.update_all(NOW + pd.Timedelta(hours=3))
        check("idempotence : re-run -> aucune double clôture",
              res2 == [] and db.open_outcomes() == [])

        # 5. SL prioritaire dans la même bougie (conservateur)
        db2 = ShadowDatabase(Path(tmp) / "s2.db")
        did2 = db2.record_decision(sig_decision(), NOW)
        ShadowTracker(db2, StubFetcher(
            m15_bars([(1.1000, 1.1095, 1.0965, 1.1005)],
                     start="2026-06-11 12:15"))).update_all(
            NOW + pd.Timedelta(hours=1))
        with db2._connect() as c:
            o = c.execute("SELECT status FROM signal_outcomes").fetchone()
        check("TP+SL même bougie -> SL retenu (conservateur)", o["status"] == "SL")

        # 6. EXPIRE après 24 h sans issue
        db3 = ShadowDatabase(Path(tmp) / "s3.db")
        did3 = db3.record_decision(
            sig_decision(entry=1.1000, sl=1.0970, tp1=1.2000), NOW)
        bars = m15_bars([(1.1000 + 0.00001 * i, 1.1004 + 0.00001 * i,
                          1.0996 + 0.00001 * i, 1.1000 + 0.00001 * i)
                         for i in range(100)], start="2026-06-11 12:15")
        ShadowTracker(db3, StubFetcher(bars)).update_all(
            NOW + pd.Timedelta(hours=30))
        with db3._connect() as c:
            o = c.execute("SELECT status, exit_price FROM signal_outcomes").fetchone()
        check("24 h sans issue -> EXPIRE au dernier close",
              o["status"] == "EXPIRE" and o["exit_price"] is not None)

        # 7. Stats §27/§36 : expectancy, PF, streak
        db4 = ShadowDatabase(Path(tmp) / "s4.db")
        cases = [
            # (tp1, bars) : 2 TP, 1 SL, 1 EXPIRE
            (1.1090, [(1.1000, 1.1095, 1.0998, 1.1090)]),
            (1.1090, [(1.1000, 1.1095, 1.0998, 1.1090)]),
            (1.1090, [(1.1000, 1.1005, 1.0965, 1.0970)]),
            (1.2000, [(1.1000, 1.1002, 1.0998, 1.1000)] * 100),
        ]
        for i, (tp1_, bars_spec) in enumerate(cases):
            d = sig_decision(tp1=tp1_)
            d.zone_id = f"Z{i}"
            now_i = NOW + pd.Timedelta(hours=4 * i)
            db4.record_decision(d, now_i)
            bars = m15_bars(bars_spec, start=str(now_i + pd.Timedelta(minutes=15)))
            ShadowTracker(db4, StubFetcher(bars)).update_all(
                now_i + pd.Timedelta(hours=30))
        stats = db4.stats()
        check("stats : 4 clôturés (2 TP/1 SL/1 EXPIRE), expectancy +1.25R, PF 6.0",
              stats["closed"] == 4 and stats["tp"] == 2 and stats["sl"] == 1
              and stats["expire"] == 1
              and abs(stats["expectancy_r"] - 1.25) < 0.01
              and stats["profit_factor"] == 6.0,
              f"expectancy {stats['expectancy_r']} PF {stats['profit_factor']}")

        # 7bis. §28 : les excursions APRÈS la sortie ne comptent pas
        #       (EXPIRE à +0.22R puis marché qui chute 2R -> MAE doit rester
        #        celui d'avant la sortie, pas -2R)
        db5 = ShadowDatabase(Path(tmp) / "s5.db")
        d5 = sig_decision(entry=1.1000, sl=1.0970, tp1=1.2000)  # jamais touché
        db5.record_decision(d5, NOW)
        bars5 = m15_bars([
            (1.1000, 1.1008, 1.0995, 1.1002),   # +0.27R favorable
        ] + [
            (1.1000, 1.1005, 1.0998, 1.1000),
        ] * 96, start="2026-06-11 12:15")        # 24 h -> EXPIRE ~ +0R
        bars_late = m15_bars([
            (1.0900, 1.0905, 1.0880, 1.0890),   # chute -4R APRÈS l'expiration
        ] * 5, start="2026-06-12 13:00")
        all_bars = pd.concat([bars5, bars_late])
        ShadowTracker(db5, StubFetcher(all_bars)).update_all(
            NOW + pd.Timedelta(hours=40))
        with db5._connect() as c:
            o = c.execute("SELECT status, mae_r, mfe_r FROM signal_outcomes"
                          ).fetchone()
        check("§28 : MAE/MFE stoppés à la sortie (chute postérieure ignorée)",
              o["status"] == "EXPIRE" and o["mae_r"] > -1.0 and o["mfe_r"] < 1.0,
              f"mae {o['mae_r']} mfe {o['mfe_r']}")

        # 8. history_records : format anti-overtrading (rejeu papier)
        hist = db4.history_records()
        check("history_records -> SignalRecords avec exits",
              len(hist) == 4 and all(h.zone_id for h in hist)
              and sum(1 for h in hist if h.exit_kind == "TP") == 2)

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Paper Shadow Mode validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
