"""Tests du Market Regime Engine — PHASE 2 (synthétique, 100 % hors-ligne).

Chaque régime de la spec est construit à la main avec des valeurs exactes,
puis classifié. Un échec ici bloque la phase (spec §35 : tests obligatoires).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.regime import (
    REGIME_BREAKOUT,
    REGIME_BREAKOUT_RETEST,
    REGIME_CHAOTIC,
    REGIME_INSUFFICIENT,
    REGIME_RANGE,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    REGIME_TRANSITION,
    MarketRegimeEngine,
)

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def to_df(closes: list[float], spread: float = 0.0004) -> pd.DataFrame:
    """Construit des bougies OHLC à partir d'une série de clôtures."""
    rows = []
    prev = closes[0]
    for c in closes:
        hi = max(prev, c) + spread
        lo = min(prev, c) - spread
        rows.append((prev, hi, lo, c))
        prev = c
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="4h", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def trend_up(n=170, start=1.1000, drift=0.0009, amp=0.0060, period=30):
    """Dérive haussière + pullbacks RÉELS (les fractales existent)."""
    return [start + drift * i + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def trend_down(n=170, start=1.3200, drift=0.0009, amp=0.0060, period=30):
    return [start - drift * i - amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def rangef(n=170, mid=1.1000, amp=0.0050, period=14):
    return [mid + amp / 2 * math.sin(2 * math.pi * i / period) for i in range(n)]


def main() -> int:
    engine = MarketRegimeEngine()
    print("=" * 70)
    print(" Tests Market Regime Engine — PHASE 2 (séries synthétiques)")
    print("=" * 70)

    # 1. INSUFFICIENT_DATA
    r = engine.detect(to_df(rangef(40)))
    check("40 bougies -> INSUFFICIENT_DATA", r.regime == REGIME_INSUFFICIENT, r.detail)

    # 2. TREND_UP / TREND_DOWN
    r = engine.detect(to_df(trend_up()))
    check("série haussière -> TREND_UP", r.regime == REGIME_TREND_UP, r.detail)
    r = engine.detect(to_df(trend_down()))
    check("série baissière -> TREND_DOWN", r.regime == REGIME_TREND_DOWN, r.detail)

    # 3. RANGE + position médiane + premium/discount
    r = engine.detect(to_df(rangef()))
    ok = (r.regime == REGIME_RANGE and r.range_high is not None
          and abs(r.range_high - 1.1025) < 0.0008 and abs(r.range_low - 1.0975) < 0.0008
          and 30 <= r.position_pct <= 70)
    check("oscillation bornée -> RANGE (bornes + position médiane)", ok,
          f"{r.detail} | pos {r.position_pct}%")

    # 4. Prix au haut du range -> premium
    closes = rangef()
    closes[-1] = 1.1024  # dernier close proche du haut
    r = engine.detect(to_df(closes))
    check("prix au haut du range -> position > 70% (premium)",
          r.regime == REGIME_RANGE and r.position_pct > 70,
          f"pos {r.position_pct}% ({r.premium_discount})")

    # 5. BREAKOUT accepté (haussier)
    closes = rangef(n=140) + [1.1000 + 0.0030 * (i + 1) for i in range(10)]
    r = engine.detect(to_df(closes))
    check("range + cassure acceptée -> BREAKOUT (direction up)",
          r.regime == REGIME_BREAKOUT and r.breakout_direction == "up", r.detail)

    # 6. BREAKOUT_RETEST (cassure puis réintégration)
    closes = rangef(n=140) + [1.1000 + 0.0030 * (i + 1) for i in range(5)]
    closes += [1.1024 - 0.0002 * i for i in range(5)]  # retombe DANS le range
    r = engine.detect(to_df(closes))
    check("cassure puis réintégration -> BREAKOUT_RETEST",
          r.regime == REGIME_BREAKOUT_RETEST, r.detail)

    # 7. TRANSITION (trend puis retournement récent)
    closes = trend_up(n=110) + [trend_up(110)[-1] - 0.0024 * (i + 1) for i in range(16)]
    r = engine.detect(to_df(closes))
    check("trend haussier + retournement récent -> TRANSITION",
          r.regime == REGIME_TRANSITION, r.detail)

    # 8. CHAOTIC (jambes de 3 bougies alternées et croissantes : chaque
    #    retournement clôture AU-DELÀ de l'extrême opposé précédent)
    closes, price = [], 1.1000
    for leg in range(56):
        move = (0.0080 + 0.0006 * leg) / 3.0
        for _ in range(3):
            price += move if leg % 2 == 0 else -move
            closes.append(price)
    r = engine.detect(to_df(closes))
    check("alternances violentes -> CHAOTIC", r.regime == REGIME_CHAOTIC, r.detail)

    # 8bis. Grind haussier ERRANT (swings muets, EMA nettes) — bruit LCG
    #        déterministe lissé : les maxima locaux varient, la majorité de
    #        swings ne peut pas voter, seule la preuve EMA tranche.
    def wandering(seed, n, amp, smooth):
        state = seed
        draws = []
        for _ in range(n):
            state = (1664525 * state + 1013904223) % (2**31)
            draws.append(state / 2**31 - 0.5)
        out, acc = [], []
        for d in draws:
            acc.append(d)
            if len(acc) > smooth:
                acc.pop(0)
            out.append(sum(acc) / len(acc) * amp)
        return out

    noise = wandering(42, 170, 0.006, 14)
    closes = [1.1000 + 0.00016 * i + noise[i] for i in range(170)]
    r = engine.detect(to_df(closes))
    check("grind haussier errant -> TREND_UP (preuve EMA)",
          r.regime == REGIME_TREND_UP and "EMA" in r.detail, r.detail[:70])
    closes_dn = [1.3200 - 0.00016 * i + noise[i] for i in range(170)]
    r = engine.detect(to_df(closes_dn))
    check("grind baissier errant -> TREND_DOWN (preuve EMA)",
          r.regime == REGIME_TREND_DOWN, r.detail[:70])

    # 9. Stabilité : un trend ne doit pas clignoter
    df = to_df(trend_up(n=300))
    regimes = [engine.detect(df.iloc[:i + 1]).regime
               for i in range(80, 300, 4)]
    flips = sum(1 for a, b in zip(regimes, regimes[1:]) if a != b)
    check("TREND_UP stable (≤ 2 changements sur 55 évaluations)",
          flips <= 2, f"{flips} changements")

    # 10. Un range ne doit pas devenir TREND par bruit
    df = to_df(rangef(n=300))
    r = engine.detect(df)
    check("range long reste RANGE (pas de trend fantôme)",
          r.regime == REGIME_RANGE, r.detail)

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Regime Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
