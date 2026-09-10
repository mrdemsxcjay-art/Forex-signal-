"""Tests du Dynamic Risk/Target Engine — PHASE 8 (synthétique, hors-ligne)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.liquidity_engine import (
    LiquidityInfo,
    LiquidityLevel,
    SweepEvent,
)
from src.analysis.regime import REGIME_RANGE, REGIME_TREND_UP, RegimeInfo
from src.analysis.risk_target_engine import (
    INVALID_RR,
    INVALID_TIGHT,
    RiskTargetEngine,
)
from src.analysis.smc_engine import SMCZone

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


TS = pd.Timestamp("2026-06-11", tz="UTC")
PRICE = 1.1000
ATR = 0.0010


def zone_bull(bottom=1.0950, top=1.0960):
    return SMCZone("OB-0001", "OB", "bullish", top, bottom, TS, "active",
                   displacement_atr=1.2, touches=0)


def liq(sweep_low=1.0940, wick=0.7, levels_above=(1.1020, 1.1080, 1.1150),
        pwh=None):
    return LiquidityInfo(
        recent_sweeps=[SweepEvent(sweep_low, "EQL", "swept_lows", TS, wick,
                                  "bullish", "test")],
        levels=[LiquidityLevel(p, "EQH" if i == 0 else "SWING_HIGH",
                               "internal" if i == 0 else "external", "untouched")
                for i, p in enumerate(levels_above)],
        key_levels={"PWH": LiquidityLevel(pwh, "PWH", "external", "untouched")}
        if pwh else {})


def regime(rng=False, trend=False, rh=1.1120, rl=1.0900, pos=30.0):
    if rng:
        return RegimeInfo(REGIME_RANGE, 0.9, "range test", rh, rl,
                          (rh + rl) / 2, pos, "discount")
    if trend:
        return RegimeInfo(REGIME_TREND_UP, 0.8, "trend test")
    return RegimeInfo(REGIME_RANGE, 0.9, "range test", rh, rl, (rh + rl) / 2,
                      pos, "discount")


def m15(lows=1.0938, highs=1.1015, n=30):
    """Série PLATE : tail(10) high/low EXACTS (= paramètres), déterministe."""
    idx = pd.date_range("2026-06-10", periods=n, freq="15min", tz="UTC")
    mid = (lows + highs) / 2
    df = pd.DataFrame({"open": [mid] * n, "high": [highs] * n,
                       "low": [lows] * n, "close": [mid] * n}, index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def h4(n=60):
    idx = pd.date_range("2026-06-01", periods=n, freq="4h", tz="UTC")
    base = [1.1000 + 0.0004 * (i % 2) for i in range(n)]
    df = pd.DataFrame({"open": base, "high": [b + 0.0005 for b in base],
                       "low": [b - 0.0005 for b in base], "close": base}, index=idx)
    df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]]


def engine():
    return RiskTargetEngine(atr=ATR)


def main() -> int:
    print("=" * 70)
    print(" Tests Dynamic Risk/Target Engine — PHASE 8")
    print("=" * 70)

    # 1. SL structurel : l'ancre la plus protectrice (extrême du sweep)
    LIQ = liq(levels_above=(1.1005, 1.1080, 1.1150))
    M15 = m15(lows=1.0938, highs=1.1008)
    plan = engine().build_plan("LONG", zone_bull(), PRICE, regime(),
                               LIQ, M15, h4())
    sweep_extreme = 1.0940 - 0.7 * ATR          # 1.0933
    expected_sl = sweep_extreme - 0.15 * ATR    # 1.09315
    check("SL = extrême du sweep − buffer (l'ancre la plus protectrice)",
          plan.valid and abs(plan.sl - expected_sl) < 1e-6
          and "sweep" in plan.sl_reason,
          f"SL {plan.sl:.5f} ({plan.sl_reason[:60]})")

    # 2. Entrée = bord de zone (retrait), TP1 = première liquidité
    check("entrée au bord de zone (1.0960) et TP1 = première liquidité (1.1005)",
          abs(plan.entry - 1.0960) < 1e-9 and abs(plan.tp1 - 1.1005) < 1e-4
          and "EQH" in plan.tp_reasons[0],
          f"TP1 {plan.tp1} rr1 {plan.rr1}")

    # 3. RANGE : TP2 = bornage opposé du range
    check("TP2 = bornage opposé du range (1.1120)",
          plan.tp2 is not None and abs(plan.tp2 - 1.1120) < 1e-4
          and "range" in plan.tp_reasons[1],
          f"TP2 {plan.tp2} rr2 {plan.rr2}")

    # 4. TP3 = candidat suivant (liquidité plus lointaine), ordre croissant
    check("TP3 au-delà de TP2 et RRs croissants",
          plan.tp3 is not None and plan.tp3 > plan.tp2 > plan.tp1
          and plan.rr3 > plan.rr2 > plan.rr1 >= 1.5,
          f"rr1/2/3 = {plan.rr1}/{plan.rr2}/{plan.rr3}")

    # 5. §21 : RR disponible insuffisant -> plan INVALIDE, TP non inventé
    plan = engine().build_plan("LONG", zone_bull(), PRICE, regime(),
                               liq(levels_above=(1.0968,)), m15(), h4())
    check("première liquidité à ~0.3R -> BAD_RR (aucun TP inventé)",
          not plan.valid and plan.invalid_code == INVALID_RR
          and plan.tp1 is not None and abs(plan.tp1 - 1.0968) < 1e-4,
          plan.invalid_reason[:80])

    # 6. §21 : le marché offre exactement ~1.5R -> plan VALIDE avec ce TP
    #    risque = 1.0960 - 1.09315 = 0.00285 ; 1.5R -> TP1 ≈ 1.100275
    plan = engine().build_plan("LONG", zone_bull(), PRICE, regime(),
                               liq(levels_above=(1.1003, 1.1080)), m15(), h4())
    check("le marché offre 1.5R -> plan valide, TP1 conservé tel quel",
          plan.valid and plan.rr1 is not None and 1.4 <= plan.rr1 <= 1.6,
          f"rr1 {plan.rr1} (TP1 {plan.tp1})")

    # 7. TREND : TP2 = deuxième candidat (pas le bornage range)
    plan = engine().build_plan("LONG", zone_bull(), PRICE, regime(trend=True),
                               liq(levels_above=(1.1005, 1.1080), pwh=1.1180),
                               m15(lows=1.0938, highs=1.1008), h4())
    check("TREND_UP : TP2 = structure suivante (1.1080), TP3 = PWH HTF",
          plan.valid and plan.tp2 is not None and abs(plan.tp2 - 1.1080) < 1e-4
          and plan.tp3 is not None and abs(plan.tp3 - 1.1180) < 1e-4,
          f"TP2 {plan.tp2} / TP3 {plan.tp3}")

    # 8. SHORT miroir : SL au-dessus, TP1 = liquidité en dessous
    z = SMCZone("OB-0009", "OB", "bearish", 1.1012, 1.1002, TS, "active")
    l = LiquidityInfo(
        recent_sweeps=[SweepEvent(1.1025, "EQH", "swept_highs", TS, 0.4,
                                  "bearish", "test")],
        levels=[LiquidityLevel(1.0950, "EQL", "internal", "untouched"),
                LiquidityLevel(1.0900, "SWING_LOW", "external", "untouched")],
        key_levels={"PWL": LiquidityLevel(1.0880, "PWL", "external", "untouched")})
    plan = engine().build_plan("SHORT", z, PRICE, regime(pos=80), l,
                               m15(lows=1.0930, highs=1.1020), h4())
    sweep_hi = 1.1025 + 0.4 * ATR
    check("SHORT : SL = extrême du sweep haut + buffer, TP1 = EQL 1.0950",
          plan.valid and abs(plan.sl - (sweep_hi + 0.15 * ATR)) < 1e-6
          and abs(plan.tp1 - 1.0950) < 1e-4 and plan.rr1 >= 1.5,
          f"SL {plan.sl:.5f} / TP1 {plan.tp1} / rr1 {plan.rr1}")

    # 9. Stop trop serré (zone collée au prix, pas de sweep, m15 plat)
    z = SMCZone("OB-0002", "OB", "bullish", 1.0999, 1.0998, TS, "active")
    plan = engine().build_plan("LONG", z, PRICE, regime(),
                               LiquidityInfo(levels=[
                                   LiquidityLevel(1.1100, "EQH", "internal", "untouched")]),
                               m15(lows=1.1000, highs=1.1010), h4())
    check("zone collée au prix -> SL_TROP_SERRE (bruit)",
          not plan.valid and plan.invalid_code == INVALID_TIGHT,
          plan.invalid_reason[:70])

    # 10. describe() : le plan se lit comme un humain l'attend
    plan = engine().build_plan("LONG", zone_bull(), PRICE, regime(),
                               LIQ, M15, h4())
    txt = plan.describe()
    check("describe() expose entrée/SL/TP + la raison structurelle du SL",
          "entrée" in txt and "SL" in txt and "TP1" in txt
          and "sweep" in txt and "rr" in txt, txt[:90])

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Risk/Target Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
