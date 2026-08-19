"""Test fonctionnel du moteur SMC — Phase 4.

Usage :  python scripts/test_smc.py

SECTIONS
  A. Séries synthétiques construites à la main : chaque pattern (sweep -> CHoCH
     -> OB -> FVG -> BOS) est vérifié sur des VALEURS EXACTES attendues.
  B. PREUVE ANTI-REPAINT : analyse de tranches croissantes ; tout événement
     émis dans une tranche doit exister À L'IDENTIQUE dans l'analyse complète
     (les états de zones ne font que progresser : touched/invalidated, fill↑).
  C. Données réelles : EURUSD/XAUUSD M15 + EURUSD H1, JSON sérialisable,
     cohérence des zones, performance temps réel.
  D. Backtest simple (CHoCH) sur données réelles.
  E. Génération du graphique interactif.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.smc import SMCEngine, SMCParams
from src.backtest.backtester import BacktestConfig, SMCBacktester
from src.data.data_fetcher import DataFetcher
from src.logger import setup_logging
from src.visualization.smc_chart import plot_smc

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(ok)
    tag = "     OK" if ok else " ÉCHEC"
    line = f"[{tag}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def approx(a, b, tol=1e-9) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
#  Série synthétique : 28 bougies à la main + queue sinusoïdale déterministe
# --------------------------------------------------------------------------- #
SYN = [
    (1.1050, 1.1058, 1.1042, 1.1044),
    (1.1044, 1.1050, 1.1030, 1.1032),
    (1.1032, 1.1038, 1.1015, 1.1018),
    (1.1018, 1.1026, 1.1010, 1.1024),
    (1.1024, 1.1035, 1.1020, 1.1033),
    (1.1033, 1.1040, 1.1016, 1.1020),
    (1.1020, 1.1028, 1.1005, 1.1008),
    (1.1008, 1.1012, 1.0992, 1.0995),
    (1.0995, 1.1006, 1.0990, 1.1004),
    (1.1004, 1.1010, 1.0998, 1.1008),
    (1.1008, 1.1015, 1.0988, 1.0992),
    (1.0992, 1.1000, 1.0991, 1.0998),
    (1.0998, 1.1008, 1.0994, 1.1006),
    (1.1006, 1.1014, 1.1000, 1.1002),
    (1.1002, 1.1008, 1.0996, 1.0997),
    (1.0997, 1.1002, 1.0986, 1.0985),   # 15 : BOS baissier (cassure du creux)
    (1.0985, 1.0994, 1.0990, 1.0992),
    (1.0992, 1.1000, 1.0989, 1.0996),
    (1.0996, 1.1006, 1.0994, 1.1002),
    (1.1002, 1.1008, 1.0998, 1.1000),   # 19 : swing high de référence
    (1.1000, 1.1004, 1.0968, 1.0994),   # 20 : SWEEP des creux égaux
    (1.0994, 1.0998, 1.0978, 1.0988),   # 21 : bougie OB (baissière)
    (1.0988, 1.1050, 1.0986, 1.1045),   # 22 : displacement -> CHoCH haussier
    (1.1045, 1.1068, 1.1022, 1.1060),   # 23 : crée le FVG haussier
    (1.1060, 1.1072, 1.1040, 1.1050),
    (1.1050, 1.1056, 1.1030, 1.1038),
    (1.1038, 1.1046, 1.1015, 1.1028),   # 26 : remplissage partiel du FVG
    (1.1028, 1.1064, 1.1024, 1.1060),
]


def synthetic_df(n_tail: int = 92) -> pd.DataFrame:
    rows = list(SYN)
    prev_close = SYN[-1][3]
    for i in range(len(SYN), len(SYN) + n_tail):
        level = 1.1058 + 0.0045 * math.sin((i - 28) / 6.0) + 0.00006 * (i - 28)
        o = prev_close
        c = level
        rows.append((o, max(o, c) + 0.0008, min(o, c) - 0.0008, c))
        prev_close = c
    index = pd.date_range("2026-08-19 00:00", periods=len(rows), freq="15min", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


# --------------------------------------------------------------------------- #
def section_a() -> None:
    print("\n--- A. Patterns synthétiques : valeurs exactes attendues ---")
    df = synthetic_df()
    engine = SMCEngine("EURUSD", "15m")
    r = engine.analyze(df)
    structure = r["events"]["structure"]
    obs = r["events"]["order_blocks"]
    fvgs = r["events"]["fair_value_gaps"]
    liq = r["events"]["liquidity"]

    # 1. CHoCH haussier à la bougie 22
    chochs_bull = [e for e in structure if e["type"] == "CHoCH" and e["direction"] == "bullish"]
    ok = bool(chochs_bull) and chochs_bull[0]["break_index"] == 22 \
        and approx(chochs_bull[0]["swing_level"], 1.1008) \
        and approx(chochs_bull[0]["break_close"], 1.1045)
    check("CHoCH haussier @ bougie 22 (cassure clôture de 1.1008)", ok,
          f"swing {chochs_bull[0]['swing_level'] if chochs_bull else '-'}, "
          f"clôture {chochs_bull[0]['break_close'] if chochs_bull else '-'}")

    # 2. BOS baissiers : premier à la bougie 6, cassure du creux à la bougie 15
    bos_bear = [e for e in structure if e["type"] == "BOS" and e["direction"] == "bearish"]
    bos_bull = [e for e in structure if e["type"] == "BOS" and e["direction"] == "bullish"]
    check("BOS baissiers @ bougies 6 et 15 (continuation)",
          any(e["break_index"] == 6 for e in bos_bear) and any(e["break_index"] == 15 for e in bos_bear),
          f"{len(bos_bear)} BOS baissiers")
    check("≥ 2 BOS haussiers de continuation ensuite", len(bos_bull) >= 2, f"{len(bos_bull)} trouvés")

    # 3. Order Block haussier = bougie 21, zone exacte
    ob = next((o for o in obs if o["direction"] == "bullish"
               and approx(o["zone_bottom"], 1.0978, 1e-9)), None)
    ok = ob is not None and approx(ob["zone_top"], 1.0998) and ob["status"] == "active" \
        and ob["break_event_id"] == chochs_bull[0]["id"]
    check("OB haussier = bougie 21, zone [1.0978, 1.0998], actif, lié au CHoCH", ok,
          f"zone [{ob['zone_bottom']}, {ob['zone_top']}]" if ob else "introuvable")

    # 4. FVG haussier [1.0998, 1.1022] créé bougie 23, remplissage ≥ 29 %
    fvg = next((f for f in fvgs if f["direction"] == "bullish"
                and approx(f["zone_bottom"], 1.0998, 1e-9)), None)
    ok = fvg is not None and approx(fvg["zone_top"], 1.1022) \
        and fvg["created_time"] == df.index[23].isoformat() and fvg["fill_pct"] >= 29
    check("FVG haussier zone [1.0998, 1.1022] créé @ bougie 23, fill ≥ 29 %", ok,
          f"fill = {fvg['fill_pct']} %" if fvg else "introuvable")

    # 5. Liquidité : pool de creux égaux (1.0987) + sweep @ bougie 20
    pools_lows = [p for p in liq["equal_lows"] if approx(p["level"], 1.0987, 1e-4)]
    check("equal lows détectés (≈1.0987, 2 creux)", len(pools_lows) >= 1,
          f"{len(pools_lows)} pool(s), niveau {pools_lows[0]['level'] if pools_lows else '-'}")
    sw = next((s for s in liq["sweeps"] if s["direction"] == "swept_lows"), None)
    ok = sw is not None and sw["index"] == 20 and approx(sw["level"], 1.0987, 1e-4)
    check("SWEEP des creux @ bougie 20 (mèche 1.0968, clôture 1.0994)", ok,
          f"niveau {sw['level']}, mèche {sw['wick_size']}" if sw else "introuvable")

    # 6. Tendance finale + JSON sérialisable
    check("tendance finale = bullish", r["trend"]["state"] == "bullish")
    try:
        payload = json.dumps(r)
        check("sortie 100 % JSON (json.dumps sans erreur)", len(payload) > 500,
              f"{len(payload):,} caractères")
    except TypeError as exc:
        check("sortie 100 % JSON", False, str(exc))

    # 7. Exemple de sortie JSON (aperçu)
    print("\n    Aperçu JSON (événement CHoCH + contexte) :")
    print("    " + json.dumps(chochs_bull[0], ensure_ascii=False, indent=2).replace("\n", "\n    "))
    print("    " + json.dumps(r["context"], ensure_ascii=False, indent=2).replace("\n", "\n    "))


# --------------------------------------------------------------------------- #
OB_KEYS = {"id", "direction", "zone_top", "zone_bottom", "origin_time", "confirmed_time", "break_event_id"}
FVG_KEYS = {"id", "direction", "zone_top", "zone_bottom", "created_time", "displacement_time"}


def section_b() -> None:
    print("\n--- B. PREUVE ANTI-REPAINT (tranches croissantes vs analyse complète) ---")
    df = synthetic_df()
    engine = SMCEngine("EURUSD", "15m")
    full = engine.analyze(df)

    f_str = {e["id"]: e for e in full["events"]["structure"]}
    f_sw = {s["id"]: s for s in full["events"]["liquidity"]["sweeps"]}
    f_ob = {o["id"]: o for o in full["events"]["order_blocks"]}
    f_fvg = {g["id"]: g for g in full["events"]["fair_value_gaps"]}

    all_ok = True
    for cut in (60, 85, 100, len(df)):
        part = engine.analyze(df.iloc[:cut])

        for e in part["events"]["structure"]:
            all_ok &= e["id"] in f_str and f_str[e["id"]] == e
        for s in part["events"]["liquidity"]["sweeps"]:
            all_ok &= s["id"] in f_sw and f_sw[s["id"]] == s
        for o in part["events"]["order_blocks"]:
            all_ok &= o["id"] in f_ob and all(o[k] == f_ob[o["id"]][k] for k in OB_KEYS)
        for g in part["events"]["fair_value_gaps"]:
            all_ok &= g["id"] in f_fvg and all(g[k] == f_fvg[g["id"]][k] for k in FVG_KEYS)
            all_ok &= g["fill_pct"] <= f_fvg[g["id"]]["fill_pct"]  # monotonie du remplissage

    check("aucun événement modifié/supprimé entre tranches et complet", all_ok,
          "4 tranches : 60/85/100/120 bougies — identités et zones strictement égales")


# --------------------------------------------------------------------------- #
def section_c() -> None:
    print("\n--- C. Données réelles (EURUSD/XAUUSD M15 + EURUSD H1) ---")
    fetcher = DataFetcher()

    for pair, tf in (("EURUSD", "15m"), ("XAUUSD", "15m"), ("EURUSD", "1h")):
        try:
            df = fetcher.get_candles(pair, tf, lookback_days=30)
            t0 = time.perf_counter()
            r = SMCEngine(pair, tf).analyze(df)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            ev = r["events"]
            zones_ok = all(o["zone_top"] > o["zone_bottom"] for o in ev["order_blocks"]) and \
                all(f["zone_top"] > f["zone_bottom"] for f in ev["fair_value_gaps"])
            chronological = all(
                ev["structure"][i]["break_index"] <= ev["structure"][i + 1]["break_index"]
                for i in range(len(ev["structure"]) - 1)
            )
            json_ok = True
            try:
                json.dumps(r)
            except TypeError:
                json_ok = False
            ok = (r["candles_analyzed"] == len(df) and zones_ok and chronological
                  and json_ok and len(ev["structure"]) >= 5
                  and len(ev["order_blocks"]) >= 3 and len(ev["fair_value_gaps"]) >= 5)
            check(f"{pair} {tf} : moteur + JSON + zones cohérentes", ok,
                  f"{len(ev['structure'])} BOS/CHoCH, {len(ev['order_blocks'])} OB, "
                  f"{len(ev['fair_value_gaps'])} FVG, {len(ev['liquidity']['sweeps'])} sweeps "
                  f"| {elapsed_ms:.0f} ms")
        except Exception as exc:  # noqa: BLE001
            check(f"{pair} {tf}", False, f"{type(exc).__name__}: {exc}")

    # Performance temps réel sur la plus grosse série
    try:
        df5 = fetcher.get_candles("EURUSD", "5m", lookback_days=30)
        t0 = time.perf_counter()
        SMCEngine("EURUSD", "5m").analyze(df5)
        elapsed = time.perf_counter() - t0
        check(f"performance {len(df5)} bougies M5 < 2,5 s", elapsed < 2.5,
              f"{elapsed * 1000:.0f} ms ({len(df5)} bougies)")
    except Exception as exc:  # noqa: BLE001
        check("performance M5", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def section_d() -> None:
    print("\n--- D. Backtest simple (CHoCH) sur EURUSD M15 réel ---")
    try:
        df = DataFetcher().get_candles("EURUSD", "15m", lookback_days=30)
        engine = SMCEngine("EURUSD", "15m")
        r = engine.analyze(df)
        stats, trades = SMCBacktester(BacktestConfig(rr=2.0, timeout_bars=48)).run(df, r)

        ok = stats["trades"] >= 1 and stats["winrate"] is not None
        if not trades.empty:
            r_values_ok = trades["r"].between(-1.01, 2.01).all()
            reasons_ok = set(trades["reason"]) <= {"TP", "SL", "timeout"}
            ok = ok and r_values_ok and reasons_ok
            out = Path("data/backtest/EURUSD_15m_trades.csv")
            out.parent.mkdir(parents=True, exist_ok=True)
            trades.to_csv(out, index=False)
        check("backtest exécuté, bornes R [-1, +2] respectées", ok,
              f"{stats['trades']} trades, winrate {stats['winrate']}, "
              f"R total {stats['total_r']}, PF {stats['profit_factor']}, "
              f"DD {stats['max_drawdown_r']}R")
    except Exception as exc:  # noqa: BLE001
        check("backtest", False, f"{type(exc).__name__}: {exc}")


def section_e() -> None:
    print("\n--- E. Graphique interactif des zones ---")
    try:
        df = DataFetcher().get_candles("EURUSD", "15m", lookback_days=30)
        engine = SMCEngine("EURUSD", "15m")
        r = engine.analyze(df)
        out = Path("data/charts/smc_EURUSD_15m.html")
        path = plot_smc(r, df, out)
        size = path.stat().st_size
        check("graphique HTML autonome généré", size > 100_000, f"{path} ({size / 1e6:.1f} Mo)")
    except Exception as exc:  # noqa: BLE001
        check("graphique", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def main() -> int:
    setup_logging(level="WARNING")
    print("=" * 70)
    print(" Test fonctionnel — moteur Smart Money Concepts (Phase 4)")
    print("=" * 70)

    section_a()
    section_b()
    section_c()
    section_d()
    section_e()

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — moteur SMC validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
