"""Tests de l'AdaptiveSignalEngine — PHASE 10 (E2E, synthétique, hors-ligne).

Le fixture `build_frames()` construit un marché complet :
    D1 grinding haussier (250 j) · H4 range sinusoïdal avec SWEEP des creux,
    jambe de cassure (crée l'OB) et repli en zone basse (~25 % du range) ·
    M15 en repli terminé par un ENGULFING haussière.
Toutes les gates doivent passer -> SIGNAL LONG (scénario B du range).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.adaptive_engine import (
    AdaptiveSignalEngine,
    EngineConfig,
)
from src.analysis.anti_overtrading_engine import SignalRecord

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def closes_to_df(closes, freq, spread=0.0004, start="2026-06-01"):
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def append_explicit(df, candles, freq):
    rows = [(r.open, r.high, r.low, r.close) for r in df.itertuples()]
    idx = list(df.index)
    step = {"4h": pd.Timedelta(hours=4), "15min": pd.Timedelta(minutes=15)}[freq]
    for o, h, l, c in candles:
        idx.append(idx[-1] + step)
        rows.append((o, h, l, c))
    out = pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                       index=pd.DatetimeIndex(idx))
    out["volume"] = 0.0
    return out[["open", "high", "low", "close", "volume"]]


def build_frames():
    d1 = closes_to_df([1.0800 + 0.00012 * i + 0.0012 * math.sin(i / 9)
                       for i in range(250)], "1D", spread=0.0004,
                      start="2025-10-22")
    sine = [1.1000 + 0.0050 * math.sin(2 * math.pi * i / 24) for i in range(210)]
    h4 = closes_to_df(sine, "4h", spread=0.0006, start="2026-05-27")
    h4 = append_explicit(h4, [
        (1.0968, 1.0972, 1.0958, 1.0962),   # descente (bougie baissière = futur OB)
        (1.0962, 1.0966, 1.0930, 1.0956),   # SWEEP des creux, close dedans
        (1.0956, 1.1018, 1.0952, 1.1015),   # reprise franche -> BOS haussier
        (1.1015, 1.1025, 1.1005, 1.1022),   # poursuite
        (1.1022, 1.1026, 1.0968, 1.0972),   # repli : clôture en zone basse
    ], "4h")
    m15_closes = [1.1005 - 0.000041 * i for i in range(110)]
    m15 = closes_to_df(m15_closes, "15min", spread=0.00025,
                       start="2026-06-29 12:00")
    m15 = append_explicit(m15, [
        (1.09598, 1.09615, 1.09580, 1.09590),
        (1.09590, 1.09725, 1.09578, 1.09705),   # ENGULFING haussière
    ], "15min")
    return {"D1": d1, "H4": h4, "M15": m15}


def main() -> int:
    frames = build_frames()
    now = frames["M15"].index[-1] + pd.Timedelta(minutes=10)
    engine = AdaptiveSignalEngine(EngineConfig())
    print("=" * 70)
    print(" Tests AdaptiveSignalEngine — PHASE 10 (E2E)")
    print("=" * 70)

    # 1. Chemin nominal : toutes les gates -> SIGNAL complet
    dec = engine.analyze(frames, now)
    check("E2E : marché aligné -> SIGNAL LONG (scénario B)",
          dec.decision == "SIGNAL" and dec.direction == "LONG"
          and dec.scenario_name == "B" and dec.code == "SETUP_VALID",
          f"score {dec.score} ({dec.grade})")

    # 2. Payload §32 complet
    check("payload §32 complet (entrée/SL/TP/RR/setup/why_now/invalidation)",
          dec.entry is not None and dec.sl is not None and dec.tp1 is not None
          and dec.rr1 is not None and dec.rr1 >= 1.5
          and "sweep" in dec.setup and dec.why_now and dec.invalidation
          and dec.market_structure and dec.liquidity_narrative
          and dec.confirmation and dec.regime,
          f"rr1 {dec.rr1} | why_now : {dec.why_now[:60]}")

    # 3. SL structurel : ancre = extrême du sweep
    check("SL motivé par l'extrême du sweep",
          "sweep" in dec.invalidation, dec.invalidation[:70])

    # 4. Idempotence (fonction pure)
    dec2 = engine.analyze(frames, now)
    check("idempotence : deux exécutions -> décision identique", dec == dec2)

    # 5. DATA_STALE (données périmées)
    dec = engine.analyze(frames, now + pd.Timedelta(hours=30))
    check("données périmées -> NO_TRADE DATA_STALE",
          dec.decision == "NO_TRADE" and dec.code == "DATA_STALE", dec.detail[:60])

    # 6. DATA_INSUFFICIENT (D1 tronqué)
    frames_bad = dict(frames)
    frames_bad["D1"] = frames["D1"].iloc[:100]
    dec = engine.analyze(frames_bad, now)
    check("D1 insuffisant -> NO_TRADE DATA_INSUFFICIENT",
          dec.decision == "NO_TRADE" and dec.code == "DATA_INSUFFICIENT")

    # 7. Anti-dérive config #1 : largeur de range max démesurée -> le range
    #    n'est plus détectable -> TRANSITION par défaut -> bloqué
    cfg = EngineConfig()
    cfg.regime = dict(cfg.regime, range_min_width_atr=50.0)
    dec = AdaptiveSignalEngine(cfg).analyze(frames, now)
    check("config consommée : range_min_width 50 ATR -> REGIME_UNCERTAIN",
          dec.decision == "NO_TRADE" and dec.code == "REGIME_UNCERTAIN",
          dec.detail[:60])

    # 8. Anti-dérive config #2 : min_rr1=5.0 -> plan impossible -> BAD_RR
    cfg = EngineConfig()
    cfg.risk = dict(cfg.risk, min_rr1=5.0)
    dec = AdaptiveSignalEngine(cfg).analyze(frames, now)
    check("config consommée : min_rr1 5.0 -> NO_TRADE BAD_RR",
          dec.decision == "NO_TRADE" and dec.code == "BAD_RR", dec.detail[:70])

    # 9. Gate NEWS injectée (réparation F-02) : news à 1 h -> blocage
    dec = engine.analyze(frames, now, news_hours_fn=lambda: 1.0)
    check("news HIGH à 1 h -> NO_TRADE NEWS_BLOCK (gate injectée, effective)",
          dec.decision == "NO_TRADE" and dec.code == "NEWS_BLOCK", dec.detail[:60])
    dec = engine.analyze(frames, now, news_hours_fn=lambda: None)
    check("news inconnues (None) -> pas de blocage",
          dec.decision == "SIGNAL")

    # 10. §18 : setup entièrement valide (score 77) mais bloqué par une gate
    #     dure -> NO_TRADE. Le score ne rachète jamais une gate.
    dec = engine.analyze(frames, now, news_hours_fn=lambda: 1.0)
    check("§18 : score élevé + gate dure -> NO_TRADE (le score ne rachète pas)",
          dec.decision == "NO_TRADE")

    # 11. ANTI-OVERTRADING : même identité déjà émise -> DUPLICATE_SETUP
    first = engine.analyze(frames, now)
    hist = [SignalRecord(zone_id=first.zone_id, direction=first.direction,
                         time=now - pd.Timedelta(hours=2))]
    dec = engine.analyze(frames, now, history=hist)
    check("identité déjà émise -> NO_TRADE DUPLICATE_SETUP",
          dec.decision == "NO_TRADE" and dec.code == "DUPLICATE_SETUP",
          dec.detail[:60])

    # 12. Seuil de qualité : min_score au-dessus du score -> SCORE_TOO_LOW
    cfg = EngineConfig()
    cfg.min_score = 90
    dec = AdaptiveSignalEngine(cfg).analyze(frames, now)
    check("min_score 90 > score 77 -> NO_TRADE SCORE_TOO_LOW",
          dec.decision == "NO_TRADE" and dec.code == "SCORE_TOO_LOW",
          dec.detail[:70])

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — AdaptiveSignalEngine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
