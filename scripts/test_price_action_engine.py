"""Tests du Price Action Engine — PHASE 5 (synthétique, hors-ligne)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from src.analysis.price_action_engine import PriceActionEngine

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def candles(rows, freq="15min", start="2026-06-01"):
    """Construit un DataFrame depuis des bougies OHLC explicites."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def calm(n=80, start=1.1000, step=0.00005, wick=0.0001):
    """Dérive ultra-calme : aucune pattern attendu."""
    rows, p = [], start
    for i in range(n):
        o, c = p, p + (step if i % 2 == 0 else -step)
        rows.append((o, max(o, c) + wick, min(o, c) - wick, c))
        p = c
    return rows


def main() -> int:
    eng = PriceActionEngine()
    print("=" * 70)
    print(" Tests Price Action Engine — PHASE 5")
    print("=" * 70)

    # 1. Pin bar haussier (mèche basse 3x le corps)
    rows = calm()
    rows[-1] = (1.1000, 1.1006, 1.0982, 1.1004)   # mèche basse dominante
    info = eng.analyze(candles(rows))
    pins = [p for p in info.patterns if p.name == "pin_bar" and p.direction == "bullish"]
    check("pin bar haussier détecté et quantifié",
          len(pins) == 1 and pins[0].strength > 0.5, pins[0].detail if pins else "?")

    # 2. Engulfing baissier
    rows = calm()
    rows[-2] = (1.1000, 1.1012, 1.0998, 1.1010)   # bougie haussière
    rows[-1] = (1.1010, 1.1011, 1.0988, 1.0990)   # l'avale en baissière
    info = eng.analyze(candles(rows))
    eng_ = [p for p in info.patterns if p.name == "engulfing" and p.direction == "bearish"]
    check("engulfing baissier détecté", len(eng_) == 1, eng_[0].detail if eng_ else "?")

    # 3. Inside bar (compression locale)
    rows = calm()
    rows[-2] = (1.1000, 1.1020, 1.0990, 1.1010)
    rows[-1] = (1.1010, 1.1015, 1.0995, 1.1005)
    info = eng.analyze(candles(rows))
    ib = [p for p in info.patterns if p.name == "inside_bar"]
    check("inside bar détecté (compression locale)", len(ib) == 1)

    # 4. Displacement (corps >= 1.2 ATR)
    rows = calm()
    atr_approx = 0.0003
    rows[-1] = (1.1000, 1.1000 + 1.5 * atr_approx, 1.0999, 1.1000 + 1.4 * atr_approx)
    info = eng.analyze(candles(rows))
    disp = [p for p in info.patterns if p.name == "displacement" and p.direction == "bullish"]
    check("displacement haussier détecté", len(disp) == 1 and disp[0].strength > 0.5,
          disp[0].detail if disp else "?")

    # 5. Absorption haussière
    rows = calm()
    rows[-2] = (1.1005, 1.1008, 1.0988, 1.0990)   # baissière
    rows[-1] = (1.0990, 1.1012, 1.0986, 1.1010)   # avale tout, clôture > high précédent
    info = eng.analyze(candles(rows))
    abs_ = [p for p in info.patterns if p.name == "absorption" and p.direction == "bullish"]
    check("absorption haussière détectée", len(abs_) == 1, abs_[0].detail if abs_ else "?")

    # 6. Compression de fenêtre (ATR5/ATR50 <= 0.6)
    rows = []
    p = 1.1000
    for i in range(70):                            #historique large
        o, c = p, p + (0.0015 if i % 2 == 0 else -0.0015)
        rows.append((o, max(o, c) + 0.0005, min(o, c) - 0.0005, c))
        p = c
    for i in range(8):                             # récent étroit
        o, c = p, p + (0.00012 if i % 2 == 0 else -0.00012)
        rows.append((o, max(o, c) + 0.00003, min(o, c) - 0.00003, c))
        p = c
    info = eng.analyze(candles(rows))
    check("compression détectée (ATR5/ATR50)",
          info.compressed and info.behavior_detail["volatility"].startswith("ATR5"),
          info.behavior_detail["volatility"])

    # 7. Expansion de fenêtre
    rows = []
    p = 1.1000
    for i in range(70):                            # calme
        o, c = p, p + (0.00012 if i % 2 == 0 else -0.00012)
        rows.append((o, max(o, c) + 0.00003, min(o, c) - 0.00003, c))
        p = c
    for i in range(6):                             # violent
        o, c = p, p + 0.0015
        rows.append((o, max(o, c) + 0.0003, min(o, c) - 0.0003, c))
        p = c
    info = eng.analyze(candles(rows))
    check("expansion détectée", info.expanding, info.behavior_detail["volatility"])

    # 8. Impulse haussière (3 bougies cumulées >= 2 ATR)
    rows = []
    p = 1.1000
    for i in range(70):
        o, c = p, p + (0.00012 if i % 2 == 0 else -0.00012)
        rows.append((o, max(o, c) + 0.00003, min(o, c) - 0.00003, c))
        p = c
    for i in range(4):
        o, c = p, p + 0.0010
        rows.append((o, c + 0.00005, o - 0.00002, c))
        p = c
    info = eng.analyze(candles(rows))
    check("impulse haussière détectée",
          info.behaviors.get("impulse") and info.impulse_direction == "bullish",
          info.behavior_detail.get("impulse", "?"))

    # 9. Consolidation (bande étroite sur 10 bougies)
    rows = []
    p = 1.1000
    for i in range(70):
        o, c = p, p + (0.00012 if i % 2 == 0 else -0.00012)
        rows.append((o, max(o, c) + 0.00003, min(o, c) - 0.00003, c))
        p = c
    for i in range(12):                            # bande très étroite
        c = 1.1000 + 0.00002 * (i % 3)
        rows.append((1.1000, 1.10012, 1.09995, c))
    info = eng.analyze(candles(rows))
    check("consolidation détectée", info.behaviors.get("consolidation"),
          info.behavior_detail["consolidation"])

    # 10. Rejection d'un niveau supérieur (bearish)
    rows = calm()
    rows[-1] = (1.1040, 1.1060, 1.1035, 1.1042)    # mèche au-dessus de 1.1050
    info = eng.analyze(candles(rows), levels=[1.1050])
    rej = [p for p in info.patterns if p.name == "rejection" and p.direction == "bearish"]
    check("rejection d'un niveau supérieur -> bearish", len(rej) == 1,
          rej[0].detail if rej else "?")

    # 11. Failed breakout (clôture au-delà puis réintégration)
    rows = calm()
    rows[-2] = (1.1040, 1.1048, 1.1038, 1.1056)    # clôture AU-DELÀ de 1.1050
    rows[-1] = (1.1056, 1.1058, 1.1036, 1.1042)    # réintégration
    info = eng.analyze(candles(rows), levels=[1.1050])
    fb = [p for p in info.patterns
          if p.name == "failed_breakout" and p.direction == "bearish"]
    check("failed breakout baissier détecté (trap du niveau 1.1050)",
          len(fb) == 1, fb[0].detail if fb else "?")

    # 12. Changement de caractère (clôture sous swing low + déplacement)
    rows = []
    p = 1.1000
    for i in range(60):                            # montée
        o, c = p, p + 0.00035
        rows.append((o, max(o, c) + 0.00015, min(o, c) - 0.00015, c))
        p = c
    for i in range(6):                             # pullback : crée un swing low
        o, c = p, p - 0.00045
        rows.append((o, max(o, c) + 0.00015, min(o, c) - 0.00015, c))
        p = c
    for i in range(12):                            # reprise haussière
        o, c = p, p + 0.00040
        rows.append((o, max(o, c) + 0.00015, min(o, c) - 0.00015, c))
        p = c
    rows[-1] = (p, p + 0.0001, p - 0.0120, p - 0.0110)   # chute avec déplacement
    info = eng.analyze(candles(rows))
    check("changement de caractère baissier détecté",
          info.character_change is not None
          and info.character_change.direction == "bearish",
          info.character_change.detail if info.character_change else "?")

    # 13. confirmation() agrège, mais ne déclenche JAMAIS seule (§14)
    conf_bear = info.confirmation("bearish")
    conf_bull = info.confirmation("bullish")
    check("confirmation('bearish') fournie, confirmation('bullish') absente",
          conf_bear is not None and conf_bull is None,
          f"{conf_bear.name} {conf_bear.strength:.2f}" if conf_bear else "?")

    # 14. Série neutre : aucune pattern, aucune confirmation
    info = eng.analyze(candles(calm()))
    check("série calme -> zéro pattern, zéro confirmation",
          not info.patterns and info.confirmation("bullish") is None
          and info.confirmation("bearish") is None and not info.character_change)

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Price Action Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
