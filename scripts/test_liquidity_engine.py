"""Tests du Liquidity Engine — PHASE 4 (synthétique, hors-ligne)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.liquidity_engine import (
    STATUS_BROKEN,
    STATUS_SWEPT,
    STATUS_UNTOUCHED,
    MarketLiquidityEngine,
)

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def to_df(closes, spread=0.0004, freq="4h", start="2026-06-01"):
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def append_candles(df, candles, freq="4h"):
    """Ajoute des bougies OHLC EXPLICITES (contrôle des mèches/clôtures)."""
    rows = list(df.itertuples())
    idx = list(df.index)
    last_time = idx[-1]
    for k, (o, h, l, c) in enumerate(candles):
        last_time = last_time + pd.Timedelta(hours=4)
        rows.append((last_time, o, h, l, c, 0.0))
        idx.append(last_time)
    out = pd.DataFrame([r[1:] for r in rows],
                       columns=["open", "high", "low", "close", "volume"],
                       index=pd.DatetimeIndex(idx, name="datetime"))
    return out[["open", "high", "low", "close", "volume"]]


def sine_range(n=100, mid=1.1010, amp=0.0035, period=20):
    """Oscillation exacte : highs égaux à chaque cycle (EQH), lows égaux (EQL)."""
    return [mid + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def d1_series(days=16, start="2026-06-01"):
    """Journées variées : le high d'hier diffère du high de la semaine passée
    (sinon la déduplication du moteur fusionne légitimement PDH et PWH)."""
    idx = pd.date_range(start, periods=days, freq="1D", tz="UTC")
    highs, lows = [], []
    for d, ts in enumerate(idx):
        if ts == idx[-1]:            # hier (dernier jour clôturé)
            highs.append(1.1060); lows.append(1.0925)
        elif ts == idx[9]:           # sommet de la semaine passée
            highs.append(1.1120); lows.append(1.0900)
        else:
            highs.append(1.1080); lows.append(1.0900)
    df = pd.DataFrame({"open": [1.1000] * days, "high": highs,
                       "low": lows, "close": [1.1000] * days}, index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def main() -> int:
    eng = MarketLiquidityEngine()
    print("=" * 70)
    print(" Tests Liquidity Engine — PHASE 4")
    print("=" * 70)

    # ---------- Série de base : range avec EQH (~mid+amp+spread) ----------
    base = to_df(sine_range())
    eqh_expected = 1.1010 + 0.0035 + 0.0004  # ~1.1049

    # 1. EQH/EQL détectés comme niveaux internes
    info = eng.analyze(base, d1_series())
    eqh = [l for l in info.levels if l.kind == "EQH"]
    eql = [l for l in info.levels if l.kind == "EQL"]
    check("EQH et EQL détectés (cluster ATR)",
          len(eqh) >= 1 and len(eql) >= 1
          and abs(eqh[0].price - eqh_expected) < 0.0004,
          f"EQH {eqh[0].price:.5f} ({eqh[0].detail})" if eqh else "aucun EQH")

    # 2. PDH/PDL/PWH/PWL depuis le D1
    pdh = [l for l in info.levels if l.kind == "PDH"]
    pdl = [l for l in info.levels if l.kind == "PDL"]
    pwh = [l for l in info.levels if l.kind == "PWH"]
    pwl = [l for l in info.levels if l.kind == "PWL"]
    check("PDH/PDL = high/low du dernier jour CLOTURÉ",
          len(pdh) == 1 and len(pdl) == 1
          and abs(pdh[0].price - 1.1060) < 1e-9 and abs(pdl[0].price - 1.0925) < 1e-9,
          f"PDH {pdh[0].price:.5f} / PDL {pdl[0].price:.5f}" if pdh and pdl else "?")
    check("PWH/PWL = extrêmes de la semaine passée",
          len(pwh) == 1 and len(pwl) == 1
          and abs(pwh[0].price - 1.1120) < 1e-9 and abs(pwl[0].price - 1.0900) < 1e-9,
          f"PWH {pwh[0].price:.5f} / PWL {pwl[0].price:.5f}" if pwh and pwl else "?")

    # 3. SWEEP d'EQH : mèche au-dessus, clôture revenue
    df = append_candles(base, [
        (1.1049, 1.1062, 1.1040, 1.1045),   # mèche > EQH, close revenue
        (1.1045, 1.1050, 1.1030, 1.1035),   # rejet
        (1.1035, 1.1040, 1.1020, 1.1025),
    ])
    info = eng.analyze(df, d1_series())
    eqh_lv = next((l for l in info.levels if l.kind == "EQH"), None)
    check("mèche au-dessus de l'EQH + clôture revenue -> SWEEP (implication baissière)",
          eqh_lv is not None and eqh_lv.status == STATUS_SWEPT
          and info.recent_sweeps and info.recent_sweeps[-1].implication == "bearish",
          f"statut {eqh_lv.status if eqh_lv else '?'} ; "
          f"sweep {info.recent_sweeps[-1].wick_atr if info.recent_sweeps else '-'} ATR")

    # 4. TRUE BREAKOUT : clôture au-delà + déplacement + acceptation
    df = append_candles(base, [
        (1.1045, 1.1130, 1.1040, 1.1120),   # corps large au-delà de l'EQH
        (1.1120, 1.1135, 1.1110, 1.1125),   # tient au-delà
        (1.1125, 1.1140, 1.1115, 1.1130),   # tient au-delà
    ])
    info = eng.analyze(df, d1_series())
    eqh_lv = next((l for l in info.levels if l.kind == "EQH"), None)
    brks = [b for b in info.recent_breakouts if b.accepted]
    check("clôture + déplacement + 2 clôtures d'acceptation -> TRUE BREAKOUT",
          eqh_lv is not None and eqh_lv.status == STATUS_BROKEN and len(brks) >= 1,
          f"disp {brks[0].displacement_atr} ATR" if brks else "aucune cassure acceptée")

    # 5. FAILED BREAKOUT : clôture au-delà puis réintégration -> trap/swept
    df = append_candles(base, [
        (1.1045, 1.1070, 1.1040, 1.1060),   # clôture au-delà de l'EQH...
        (1.1060, 1.1065, 1.1035, 1.1040),   # ...mais réintégration immédiate
        (1.1040, 1.1045, 1.1025, 1.1030),
    ])
    info = eng.analyze(df, d1_series())
    eqh_lv = next((l for l in info.levels if l.kind == "EQH"), None)
    check("cassure non acceptée + réintégration -> SWEEP (trap, pas un breakout)",
          eqh_lv is not None and eqh_lv.status == STATUS_SWEPT,
          f"statut {eqh_lv.status if eqh_lv else '?'}")

    # 6. Niveaux intacts les plus proches (au-dessus / en dessous)
    info = eng.analyze(base, d1_series())
    price = float(base["close"].iloc[-1])
    ok = (info.nearest_above is not None and info.nearest_above.price > price
          and info.nearest_below is not None and info.nearest_below.price < price)
    check("niveaux intacts les plus proches de part et d'autre du prix", ok,
          f"au-dessus {info.nearest_above.kind} {info.nearest_above.price:.5f} / "
          f"en dessous {info.nearest_below.kind} {info.nearest_below.price:.5f}"
          if ok else info.narrative)

    # 7. Narratif §11 : question « prise ou en route ? »
    check("narratif répond à la question §11 (liquide prise / en route)",
          ("PRENDRE" in info.narrative or "AU-DESSUS" in info.narrative)
          and "ATR" in info.narrative, info.narrative[:90])

    # 8. CAUSALITÉ : mêmes événements quand on rejoue la fenêtre tronquée
    full = eng.analyze(append_candles(base, [
        (1.1045, 1.1062, 1.1040, 1.1045),
        (1.1045, 1.1050, 1.1030, 1.1035)]), d1_series())
    truncated = eng.analyze(append_candles(base, [
        (1.1045, 1.1062, 1.1040, 1.1045)]), d1_series())  # arrêt 1 bougie avant
    same = (full.recent_sweeps[0].time == truncated.recent_sweeps[0].time
            and full.recent_sweeps[0].level_price == truncated.recent_sweeps[0].level_price)
    check("causalité : le sweep détecté ne dépend pas des bougies futures", same,
          f"sweep @ {full.recent_sweeps[0].time} dans les deux fenêtres")

    # 9. Statuts par ajout (jamais de retrait) — anti-repaint des statuts
    lv_a = {l.kind: l.status for l in truncated.levels}
    lv_b = {l.kind: l.status for l in eng.analyze(base, d1_series()).levels}
    ok = all(lv_a.get(k) in (v, STATUS_SWEPT, STATUS_BROKEN)
             for k, v in lv_b.items() if k in lv_a)
    check("les statuts ne font que progresser (untouched -> swept/broken)", ok)

    # 10. Sweep des lows -> implication haussière (question §11 inversée)
    eql_expected = 1.1010 - 0.0035 - 0.0004
    df = append_candles(base, [
        (1.0975, 1.0980, 1.0955, 1.0970),   # mèche sous l'EQL, close revenue
        (1.0970, 1.0995, 1.0965, 1.0990),
    ])
    info = eng.analyze(df, d1_series())
    check("mèche sous l'EQL -> SWEEP haussier (just_swept_lows)",
          info.just_swept_lows
          and info.recent_sweeps[-1].implication == "bullish"
          and abs(info.recent_sweeps[-1].level_price - eql_expected) < 0.0004,
          f"niveau {info.recent_sweeps[-1].level_price:.5f}" if info.recent_sweeps else "?")

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Liquidity Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
