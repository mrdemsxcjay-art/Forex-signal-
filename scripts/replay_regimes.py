"""Replay étiqueté du Regime Engine — PHASE 2 (données réelles).

1. Distribution des régimes sur 60 jours de clôtures H4 EUR/USD
2. Stabilité (taux de changement de régime)
3. CONTRE-FACTUEL : régime en vigueur au moment de CHACUN des 16 signaux
   réels émis par le moteur actuel (semaine du 5-9 sept, 0 TP / 10 SL)
4. Mini-grille de calibration (spec §30) : candidats -> stabilité + exactitude
"""
from __future__ import annotations

import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.regime import MarketRegimeEngine
from src.data.data_fetcher import DataFetcher
from src.logger import setup_logging

PAGE = "https://mrdemsxcjay-art.github.io/Forex-signal-/"


def load_real_signals(since="2026-09-05 12"):
    """Signaux réels depuis la page de statut (base du moteur live)."""
    html = urllib.request.urlopen(PAGE).read().decode()
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) != 10:
            continue
        c = [re.sub(r"<[^>]+>", "", x).strip() for x in cells]
        if c[0] >= since:
            out.append((c[0], c[1], c[2], c[3], c[8]))  # date, paire, sens, score, issue
    return out


def main() -> int:
    setup_logging(level="ERROR")
    print("Chargement EURUSD H4 (120 jours)...")
    h4 = DataFetcher().get_candles("EURUSD", "4h", lookback_days=120)
    print(f"{len(h4)} bougies H4 ({h4.index[0]:%Y-%m-%d} -> {h4.index[-1]:%Y-%m-%d})")

    engine = MarketRegimeEngine()

    # ---- 1) Distribution sur 60 jours ------------------------------------
    start = h4.index[-1] - pd.Timedelta(days=60)
    stamps = [t for t in h4.index if t >= start]
    regimes = [engine.detect(h4.loc[:t]).regime for t in stamps]
    dist = Counter(regimes)
    flips = sum(1 for a, b in zip(regimes, regimes[1:]) if a != b)
    print(f"\n=== DISTRIBUTION (60 jours, {len(stamps)} clôtures H4) ===")
    for r, n in dist.most_common():
        print(f"  {r:<18} {n:4d}  ({n / len(stamps) * 100:.0f} %)")
    print(f"changements de régime : {flips} ({flips / len(stamps) * 100:.0f} % des clôtures, "
          f"~{flips / 8.5:.1f}/semaine)")

    # ---- 2) Contre-factuel des signaux réels ------------------------------
    signals = load_real_signals()
    print(f"\n=== CONTRE-FACTUEL : {len(signals)} signaux réels du moteur actuel ===")
    blocked = 0
    for date, paire, sens, score, issue in signals:
        ts = pd.Timestamp(date, tz="UTC")
        r = engine.detect(h4.loc[:ts])
        would_block = r.regime in ("RANGE", "TRANSITION", "CHAOTIC",
                                   "BREAKOUT_RETEST", "INSUFFICIENT_DATA")
        blocked += would_block
        print(f"  {date}  {sens:<5} {score:>2}/100 ({issue:<12}) "
              f"-> régime {r.regime:<15} {'BLOQUÉ' if would_block else 'passé aux gates'}"
              f"  [{r.detail[:60]}]")
    print(f"\n=> {blocked}/{len(signals)} signaux auraient été bloqués/en attente "
          f"par le seul filtre de régime ({blocked / max(len(signals), 1) * 100:.0f} %)")

    # ---- 3) Mini-grille de calibration ------------------------------------
    print("\n=== CALIBRATION (candidats, spec §30) ===")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_regime import to_df, trend_up, trend_down, rangef

    synth = {
        "TREND_UP": to_df(trend_up()), "TREND_DOWN": to_df(trend_down()),
        "RANGE": to_df(rangef()),
        "BREAKOUT": to_df(rangef(n=140) + [1.1000 + 0.0030 * (i + 1) for i in range(10)]),
    }
    synth_expected = {"TREND_UP": "TREND_UP", "TREND_DOWN": "TREND_DOWN",
                      "RANGE": "RANGE", "BREAKOUT": "BREAKOUT"}
    candidates = [
        ("C1 permissif", dict(trend_majority=0.6, range_touch_tol_atr=0.5, range_boundary_drift_atr=0.5)),
        ("C2 défaut   ", dict(trend_majority=0.7, range_touch_tol_atr=0.5, range_boundary_drift_atr=0.5)),
        ("C3 strict   ", dict(trend_majority=0.8, range_touch_tol_atr=0.5, range_boundary_drift_atr=0.5)),
        ("C4 touches+ ", dict(trend_majority=0.7, range_touch_tol_atr=0.25, range_boundary_drift_atr=0.5)),
        ("C5 bornes+  ", dict(trend_majority=0.7, range_touch_tol_atr=0.5, range_boundary_drift_atr=1.0)),
    ]
    print(f"{'candidat':<13} {'synthétiques':<13} {'flips/sem':<10} {'part régime dominant':<20}")
    best = None
    for name, params in candidates:
        eng = MarketRegimeEngine(**params)
        ok = all(eng.detect(df).regime == synth_expected[k] for k, df in synth.items())
        regs = [eng.detect(h4.loc[:t]).regime for t in stamps]
        f = sum(1 for a, b in zip(regs, regs[1:]) if a != b) / 8.5
        top, top_n = Counter(regs).most_common(1)[0]
        print(f"{name:<13} {'4/4' if ok else 'ÉCHEC':<13} {f:<10.1f} "
              f"{top} {top_n / len(regs) * 100:.0f}%")
        if ok and (best is None or f < best[2]):
            best = (name, params, f)
    print(f"\nchoix : {best[0]} (stabilité max, exactitude synthétique conservée) "
          f"— robustesse hors-échantillon à confirmer en PHASE 12")
    return 0


if __name__ == "__main__":
    sys.exit(main())
