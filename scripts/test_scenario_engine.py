"""Tests du Scenario + Location Engine — PHASE 7 (synthétique, hors-ligne)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.liquidity_engine import LiquidityInfo, LiquidityLevel, SweepEvent
from src.analysis.price_action_engine import Pattern, PriceActionInfo
from src.analysis.regime import (
    REGIME_RANGE,
    REGIME_TREND_UP,
    RegimeInfo,
)
from src.analysis.scenario_engine import (
    NT_EXTENDED_MOVE,
    NT_HTF_CONFLICT,
    NT_MID_RANGE,
    NT_NO_CONFIRMATION,
    NT_NO_ZONE,
    NT_REGIME_UNCERTAIN,
    OK_SETUP,
    ScenarioEngine,
)
from src.analysis.smc_engine import SMCIntegrationEngine, SMCZone
from src.analysis.structure_engine import StructureVerdict

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


PRICE = 1.1000
TS = pd.Timestamp("2026-06-11", tz="UTC")


def zone_bull():
    """Zone de demande à ~0.7 ATR SOUS le prix (à portée, retrait possible)."""
    return SMCZone("OB-0001", "OB", "bullish", 1.0998, 1.0988, TS, "active",
                   displacement_atr=1.2, touches=0)


def zone_bear():
    """Zone d'offre à ~0.7 ATR AU-DESSUS du prix."""
    return SMCZone("OB-0009", "OB", "bearish", 1.1012, 1.1002, TS, "active",
                   displacement_atr=1.2, touches=0)


def regime_range(pos):
    return RegimeInfo(REGIME_RANGE, 0.9, "range de test", 1.1120, 1.0900,
                      1.1010, pos, "discount" if pos < 45 else "premium")


def regime_trend_up():
    return RegimeInfo(REGIME_TREND_UP, 0.8, "trend up de test")


def regime_transition():
    return RegimeInfo("TRANSITION", 0.6, "transition de test")


def verdict(direction):
    align = {"LONG": "ALIGNED_UP", "SHORT": "ALIGNED_DOWN", None: "EXTERNAL_UNCLEAR"}
    trend = {"LONG": "bullish", "SHORT": "bearish", None: None}
    return StructureVerdict(trend[direction], trend[direction],
                            align[direction], direction, "test")


def liq(direction):
    """Carburant + objectif + niveaux jour (pour la Daily Map)."""
    if direction == "LONG":
        return LiquidityInfo(
            recent_sweeps=[SweepEvent(1.0940, "EQL", "swept_lows", TS, 0.7,
                                      "bullish", "test")],
            nearest_above=LiquidityLevel(1.1030, "EQH", "internal", "untouched"),
            levels=[LiquidityLevel(1.1080, "PDH", "external", "untouched"),
                    LiquidityLevel(1.0940, "PDL", "external", "untouched")])
    return LiquidityInfo(
        recent_sweeps=[SweepEvent(1.1110, "EQH", "swept_highs", TS, 0.7,
                                  "bearish", "test")],
        nearest_below=LiquidityLevel(1.0960, "EQL", "internal", "untouched"),
        levels=[LiquidityLevel(1.1080, "PDH", "external", "untouched"),
                LiquidityLevel(1.0940, "PDL", "external", "untouched")])


def pa(direction):
    info = PriceActionInfo()
    if direction:
        info.patterns = [Pattern("engulfing", direction, 99, TS, 0.8, "test")]
    return info


def frames(daily_range=0.0030, today_range=0.0015):
    """D1 calme (amplitude constante) + H4 dont la journée en cours a une
    amplitude maîtrisée (pour la règle EXTENDED_MOVE)."""
    days = 25
    idx = pd.date_range(end="2026-06-30", periods=days, freq="1D", tz="UTC")
    d1 = pd.DataFrame({"open": [1.10] * days, "high": [1.10 + daily_range] * days,
                       "low": [1.10] * days, "close": [1.10] * days}, index=idx)
    d1["volume"] = 0.0
    # H4 : 60 bougies calmes, la dernière journée = 3 bougies données
    base = pd.Timestamp("2026-06-30", tz="UTC")
    h4_idx, rows = [], []
    p = 1.1000
    for i in range(57):
        h4_idx.append(base - pd.Timedelta(hours=4 * (57 - i)))
        rows.append((p, p + 0.0006, p - 0.0006, p))
    for k in range(3):  # aujourd'hui : 3 bougies
        h4_idx.append(base + pd.Timedelta(hours=4 * k))
        rows.append((1.1000, 1.1000 + today_range / 2,
                     1.1000 - today_range / 2, 1.1000 + 0.0002))
    h4 = pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex(h4_idx))
    h4["volume"] = 0.0
    return d1[["open", "high", "low", "close", "volume"]], \
        h4[["open", "high", "low", "close", "volume"]]


def engine():
    return ScenarioEngine(h4_atr=0.0010)   # ATR injecté : tests déterministes


def main() -> int:
    smc = SMCIntegrationEngine()
    d1, h4 = frames()
    print("=" * 70)
    print(" Tests Scenario + Location Engine — PHASE 7")
    print("=" * 70)

    # 1. RANGE bas + zone demande + sweep lows + confirmation -> SIGNAL LONG
    dec = engine().decide(PRICE, regime_range(30), verdict("LONG"), liq("LONG"),
                          pa("bullish"), [zone_bull()], d1, h4, smc)
    check("RANGE bas + scénario B complet -> SIGNAL LONG",
          dec.decision == "SIGNAL" and dec.direction == "LONG"
          and dec.reason_code == OK_SETUP and dec.scenario.name == "B"
          and dec.zone.id == "OB-0001",
          f"{dec.decision}/{dec.reason_code} scénario {dec.scenario.name if dec.scenario else '?'}")

    # 2. RANGE haut + zone offre + sweep highs -> SIGNAL SHORT
    dec = engine().decide(PRICE, regime_range(80), verdict("SHORT"), liq("SHORT"),
                          pa("bearish"), [zone_bear()], d1, h4, smc)
    check("RANGE haut + scénario A complet -> SIGNAL SHORT",
          dec.decision == "SIGNAL" and dec.direction == "SHORT"
          and dec.scenario.name == "A",
          f"{dec.decision}/{dec.reason_code}")

    # 3. MILIEU de range -> NO_TRADE MID_RANGE (§5)
    dec = engine().decide(PRICE, regime_range(50), verdict("LONG"), liq("LONG"),
                          pa("bullish"), [zone_bull()], d1, h4, smc)
    check("MILIEU de range -> NO_TRADE MID_RANGE",
          dec.decision == "NO_TRADE" and dec.reason_code == NT_MID_RANGE,
          dec.detail)

    # 4. TRANSITION -> NO_TRADE REGIME_UNCERTAIN
    dec = engine().decide(PRICE, regime_transition(), verdict("LONG"), liq("LONG"),
                          pa("bullish"), [zone_bull()], d1, h4, smc)
    check("régime TRANSITION -> NO_TRADE REGIME_UNCERTAIN",
          dec.decision == "NO_TRADE" and dec.reason_code == NT_REGIME_UNCERTAIN)

    # 5. Aucune zone à portée -> NO_TRADE NO_ZONE
    dec = engine().decide(PRICE, regime_range(30), verdict("LONG"), liq("LONG"),
                          pa("bullish"), [], d1, h4, smc)
    check("aucune zone à portée -> NO_TRADE NO_ZONE",
          dec.decision == "NO_TRADE" and dec.reason_code == NT_NO_ZONE, dec.detail)

    # 6. Zone présente mais sans confirmation -> NO_TRADE NO_CONFIRMATION
    dec = engine().decide(PRICE, regime_range(30), verdict("LONG"), liq("LONG"),
                          pa(None), [zone_bull()], d1, h4, smc)
    check("scénario armé sans confirmation -> NO_TRADE NO_CONFIRMATION",
          dec.decision == "NO_TRADE" and dec.reason_code == NT_NO_CONFIRMATION,
          dec.detail[:70])

    # 7. Structure HTF opposée au scénario -> HTF_CONFLICT
    dec = engine().decide(PRICE, regime_range(30), verdict("SHORT"), liq("LONG"),
                          pa("bullish"), [zone_bull()], d1, h4, smc)
    check("scénario LONG mais structure autorise SHORT -> HTF_CONFLICT",
          dec.decision == "NO_TRADE" and dec.reason_code == NT_HTF_CONFLICT)

    # 8. TREND_UP + retrait en zone + confirmation -> SIGNAL LONG (continuation)
    dec = engine().decide(PRICE, regime_trend_up(), verdict("LONG"), liq("LONG"),
                          pa("bullish"), [zone_bull()], d1, h4, smc)
    check("TREND_UP + retrait en zone -> SIGNAL LONG (scénario T)",
          dec.decision == "SIGNAL" and dec.direction == "LONG"
          and dec.scenario.name == "T",
          f"{dec.decision}/{dec.reason_code}")

    # 9. EXTENDED_MOVE : amplitude du jour 2.7x l'attendue -> NO_TRADE (§23)
    d1_big, h4_big = frames(today_range=0.0080)
    dec = engine().decide(PRICE, regime_trend_up(), verdict("LONG"), liq("LONG"),
                          pa("bullish"), [zone_bull()], d1_big, h4_big, smc)
    check("mouvement du jour déjà consommé (267%) -> NO_TRADE EXTENDED_MOVE",
          dec.decision == "NO_TRADE" and dec.reason_code == NT_EXTENDED_MOVE,
          dec.detail[:80])

    # 10. Daily Market Map : champs remplis et lisibles (§23)
    mmap = engine().daily_map(PRICE, d1, h4, regime_range(30), liq("LONG"),
                              verdict("LONG"), [zone_bull()])
    lines = mmap.lines()
    check("Daily Market Map complète (PDH/PDL, range, OB, ratio amplitude)",
          mmap.pdh is not None and mmap.range_high is not None
          and mmap.major_ob is not None and mmap.realized_ratio is not None
          and not mmap.extended_move and len(lines) >= 5,
          f"ratio {mmap.realized_ratio:.0%}")

    # 11. Scénario C (milieu) présent dans le set RANGE
    from src.analysis.scenario_engine import ScenarioEngine as SE
    loc = engine().locate(PRICE, regime_range(50), liq("LONG"), [zone_bull()], 0.0010)
    ss = engine().build_scenarios(PRICE, regime_range(50), loc, verdict("LONG"),
                                  liq("LONG"), pa("bullish"), [zone_bull()])
    names = {s.name: s.status for s in ss.scenarios}
    check("set de scénarios RANGE = A/B/C, C déclenché au milieu",
          names.get("C") == "TRIGGERED" and "A" in names and "B" in names,
          str(names))

    # 12. Location : verdict AT_LOCATION avec distance en ATR
    loc = engine().locate(PRICE, regime_range(30), liq("LONG"), [zone_bull()], 0.0010)
    check("location rapporte la zone la plus proche et sa distance",
          loc.verdict == "AT_LOCATION" and loc.zone_distance_atr is not None
          and loc.zone_distance_atr <= 1.5,
          f"{loc.detail}")

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Scenario Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
