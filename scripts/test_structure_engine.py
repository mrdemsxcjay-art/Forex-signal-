"""Tests du Market Structure Engine — PHASE 3 (synthétique, hors-ligne)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.structure_engine import (
    ALIGNED_DOWN,
    ALIGNED_UP,
    EXTERNAL_UNCLEAR,
    PULLBACK_UP,
    MarketStructureEngine,
)

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def to_df(closes, spread=0.0004, freq="4h"):
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
    idx = pd.date_range("2026-06-01", periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def trend_up(n=180, start=1.1000, drift=0.0009, amp=0.0060, period=30):
    return [start + drift * i + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def trend_down(n=180, start=1.3200, drift=0.0009, amp=0.0060, period=30):
    return [start - drift * i - amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def main() -> int:
    eng = MarketStructureEngine()
    print("=" * 70)
    print(" Tests Market Structure Engine — PHASE 3")
    print("=" * 70)

    # 1. Labellisation haussière : que des HH/HL après démarrage
    labels = [sl.label for sl in eng.label_swings(to_df(trend_up()))]
    hh_hl = [l for l in labels if l in ("HH", "HL")]
    lh_ll = [l for l in labels if l in ("LH", "LL")]
    check("trend haussier -> majorité écrasante de HH/HL",
          len(hh_hl) >= 8 and len(lh_ll) <= 2, f"HH/HL={len(hh_hl)}, LH/LL={len(lh_ll)}")

    # 2. Labellisation baissière
    labels = [sl.label for sl in eng.label_swings(to_df(trend_down()))]
    lh_ll = [l for l in labels if l in ("LH", "LL")]
    hh_hl = [l for l in labels if l in ("HH", "HL")]
    check("trend baissier -> majorité écrasante de LH/LL",
          len(lh_ll) >= 8 and len(hh_hl) <= 2, f"LH/LL={len(lh_ll)}, HH/HL={len(hh_hl)}")

    # 3. Séquence ordonnée : sur un trend up, les highs sont HH
    highs = [sl.label for sl in eng.label_swings(to_df(trend_up())) if sl.kind == "high"]
    check("tous les sommets successifs du trend up sont HH",
          highs and all(l in ("HH", "H?", "EQH") for l in highs), str(highs[:6]))

    # 4. MSS : CHoCH AVEC déplacement (jambe rapide et large)
    closes = trend_up(n=110)
    top = closes[-1]
    fast_reversal = [top - 0.0042 * (i + 1) for i in range(14)]  # bougies larges
    s_fast = eng.summarize(to_df(closes + fast_reversal), "H4")
    check("retournement VIOLENT -> MSS détecté",
          s_fast.last_mss is not None and s_fast.last_mss["direction"] == "bearish",
          f"mss: {s_fast.last_mss}")

    # 5. CHoCH sans déplacement (érosion lente) -> PAS un MSS
    closes = trend_up(n=110)
    slow_reversal = [top - 0.0010 * (i + 1) for i in range(20)]  # petites bougies
    s_slow = eng.summarize(to_df(closes + slow_reversal), "H4")
    slow_choch = (s_slow.last_event is not None and s_slow.last_event["type"] == "CHoCH")
    check("retournement LENT -> CHoCH éventuel mais AUCUN MSS",
          s_slow.last_mss is None,
          f"dernier événement: {s_slow.last_event['type'] if s_slow.last_event else 'aucun'} "
          f"(body {s_slow.last_event['body_atr'] if s_slow.last_event else '-'} ATR)")

    # 6. Hiérarchie : D1 haussier + M15 baissier -> PULLBACK, LONG seulement
    summaries = {
        "D1": eng.summarize(to_df(trend_up(180), freq="1D"), "D1"),
        "H4": eng.summarize(to_df(trend_up(180), freq="4h"), "H4"),
        "M15": eng.summarize(to_df(trend_down(120), freq="15min"), "M15"),
    }
    v = MarketStructureEngine.combine(summaries)
    check("D1/H4 haussiers + M15 baissier -> PULLBACK_IN_UPTREND, LONG autorisé",
          v.alignment == PULLBACK_UP and v.allowed_direction == "LONG", v.description)

    # 7. Alignement total haussier
    summaries["M15"] = eng.summarize(to_df(trend_up(120), freq="15min"), "M15")
    v = MarketStructureEngine.combine(summaries)
    check("tout aligné haussier -> ALIGNED_UP", v.alignment == ALIGNED_UP
          and v.allowed_direction == "LONG", v.description)

    # 8. Alignement total baissier
    summaries_dn = {
        "D1": eng.summarize(to_df(trend_down(180), freq="1D"), "D1"),
        "M15": eng.summarize(to_df(trend_down(120), freq="15min"), "M15"),
    }
    v = MarketStructureEngine.combine(summaries_dn)
    check("tout aligné baissier -> ALIGNED_DOWN, SHORT autorisé",
          v.alignment == ALIGNED_DOWN and v.allowed_direction == "SHORT", v.description)

    # 9. Externe indéterminé -> aucune direction (règle anti-forçage)
    summaries_flat = {
        "D1": eng.summarize(to_df([1.1000 + 0.0004 * math.sin(i / 6) for i in range(180)],
                                  freq="1D"), "D1"),
        "M15": eng.summarize(to_df(trend_up(120), freq="15min"), "M15"),
    }
    v = MarketStructureEngine.combine(summaries_flat)
    check("D1 indéterminé + M15 haussier -> EXTERNAL_UNCLEAR, AUCUNE direction",
          v.alignment == EXTERNAL_UNCLEAR and v.allowed_direction is None, v.description)

    # 10. La confirmation M5 n'inverse JAMAIS le D1 (spec §10)
    summaries["M5"] = eng.summarize(to_df(trend_down(80), freq="5min"), "M5")
    v = MarketStructureEngine.combine(summaries)
    check("M5 baissier ajouté au contexte haussier -> toujours LONG autorisé",
          v.allowed_direction == "LONG" and v.alignment in (ALIGNED_UP, PULLBACK_UP),
          v.alignment)

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Structure Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
