"""Tests de l'Anti-Overtrading Engine — PHASE 9 (synthétique, hors-ligne)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.anti_overtrading_engine import (
    COOLDOWN_ACTIVE,
    CONSECUTIVE_CAP,
    DAILY_CAP,
    DUPLICATE_SETUP,
    EXIT_SL,
    EXIT_TP,
    POST_SL_CAP,
    REENTRY_NO_NEW_STRUCTURE,
    ZONE_CAP,
    AntiOvertradingEngine,
    Candidate,
    ContextEvents,
    FrequencyMonitor,
    SignalRecord,
)

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


NOW = pd.Timestamp("2026-06-11 12:00", tz="UTC")
TODAY = "2026-06-11"
YESTERDAY = "2026-06-10"


def rec(zone, direction, day, hour, exit_kind=None, exit_day=None, exit_hour=None):
    return SignalRecord(
        zone_id=zone, direction=direction,
        time=pd.Timestamp(f"{day} {hour:02d}:00", tz="UTC"),
        exit_time=pd.Timestamp(f"{exit_day or day} {exit_hour or hour:02d}:00", tz="UTC")
        if exit_kind else None,
        exit_kind=exit_kind)


def main() -> int:
    eng = AntiOvertradingEngine()   # cooldowns : 180 neutre / 120 TP / 240 SL
    print("=" * 70)
    print(" Tests Anti-Overtrading Engine — PHASE 9")
    print("=" * 70)

    # 1. Setup identique répété -> DUPLICATE_SETUP (§35)
    hist = [rec("OB-1", "LONG", YESTERDAY, 8, EXIT_TP, YESTERDAY, 12)]
    dec = eng.check(Candidate("OB-1", "LONG", scenario="B"), hist, NOW)
    check("même zone + même sens (même via un autre scénario) -> DUPLICATE_SETUP",
          not dec.allowed and dec.code == DUPLICATE_SETUP, dec.detail[:70])

    # 2. Signal après SL SANS nouvelle structure -> interdit (§35)
    hist = [rec("OB-2", "LONG", TODAY, 2, EXIT_SL, TODAY, 6)]
    dec = eng.check(Candidate("OB-2", "SHORT", "A"), hist, NOW,
                    ContextEvents())   # aucun événement récent
    check("après SL sur la zone, sans nouvelle structure -> REENTRY interdit",
          not dec.allowed and dec.code == REENTRY_NO_NEW_STRUCTURE, dec.detail[:70])

    # 3. Re-entry AVEC nouvelle structure (sweep après la sortie) -> autorisé
    ctx = ContextEvents(
        sweep_times=[pd.Timestamp(f"{TODAY} 09:30", tz="UTC")])   # après SL (06:00)
    dec = eng.check(Candidate("OB-2", "SHORT", "A"), hist, NOW, ctx)
    check("nouveau sweep APRÈS la sortie -> re-entry autorisé",
          dec.allowed, dec.detail)

    # 4. Cooldown asymétrique : 3h30 après la sortie -> TP passé, SL bloqué
    MID = pd.Timestamp(f"{TODAY} 11:30", tz="UTC")
    hist_tp = [rec("OB-3", "LONG", TODAY, 4, EXIT_TP, TODAY, 8)]
    dec_tp = eng.check(Candidate("OB-4", "LONG"), hist_tp, MID)
    hist_sl = [rec("OB-3", "LONG", TODAY, 4, EXIT_SL, TODAY, 8)]
    dec_sl = eng.check(Candidate("OB-4", "LONG"), hist_sl, MID)
    check("asymétrie : 3h30 après TP = autorisé, 3h30 après SL = COOLDOWN",
          dec_tp.allowed and not dec_sl.allowed
          and dec_sl.code == COOLDOWN_ACTIVE, dec_sl.detail[:60])

    # 4bis. Même identité + NOUVELLE structure depuis le dernier signal ->
    #       re-entry autorisée (§25), c'est le coeur de la règle re-entry
    hist_re = [rec("RE-1", "LONG", TODAY, 2, EXIT_SL, TODAY, 4)]
    ctx_re = ContextEvents(
        sweep_times=[pd.Timestamp(f"{TODAY} 09:00", tz="UTC")])
    dec_re = eng.check(Candidate("RE-1", "LONG", "T"), hist_re, NOW, ctx_re)
    check("même identité + nouveau sweep depuis -> re-entry autorisée (§25)",
          dec_re.allowed, dec_re.detail)

    # 5. DAILY_CAP (§26)
    hist = [rec("Z1", "LONG", TODAY, 1), rec("Z2", "SHORT", TODAY, 4),
            rec("Z3", "LONG", TODAY, 7)]          # 3 signaux aujourd'hui
    dec = eng.check(Candidate("Z4", "LONG"), hist, NOW)
    check("3e signal du jour déjà atteint -> DAILY_CAP",
          not dec.allowed and dec.code == DAILY_CAP, dec.detail[:60])

    # 6. ZONE_CAP : 2 signaux sur la MÊME zone aujourd'hui -> bloqué
    #    (le candidat porte une nouvelle structure -> dedup/re-entry franchis)
    hist = [rec("Z9", "LONG", TODAY, 1), rec("Z9", "SHORT", TODAY, 4)]
    ctx_zone = ContextEvents(
        sweep_times=[pd.Timestamp(f"{TODAY} 11:00", tz="UTC")])
    dec = eng.check(Candidate("Z9", "LONG", "re-entry"), hist, NOW, ctx_zone)
    check("zone déjà sollicitée 2 fois aujourd'hui -> ZONE_CAP",
          not dec.allowed and dec.code == ZONE_CAP, dec.detail[:60])

    # 7. CONSECUTIVE_CAP : 3 LONG d'affilée -> le 4e bloqué, un SHORT passe
    eng7 = AntiOvertradingEngine(max_per_day=6)
    hist = [rec("A1", "LONG", TODAY, 2), rec("A2", "LONG", TODAY, 4),
            rec("A3", "LONG", TODAY, 6)]
    dec_l = eng7.check(Candidate("A4", "LONG"), hist, NOW)
    dec_s = eng7.check(Candidate("A4", "SHORT"), hist, NOW)
    check("3 LONG consécutifs -> 4e LONG bloqué, SHORT autorisé",
          not dec_l.allowed and dec_l.code == CONSECUTIVE_CAP and dec_s.allowed)

    # 8. POST_SL_CAP : trop de signaux enchaînés après des pertes
    eng8 = AntiOvertradingEngine(max_per_day=6)
    hist = [
        rec("B1", "LONG", TODAY, 1, EXIT_SL, TODAY, 3),    # perte clôturée 03:00
        rec("B2", "LONG", TODAY, 4),                        # post-SL n°1
        rec("B3", "SHORT", TODAY, 6),                       # post-SL n°2
    ]
    dec = eng8.check(Candidate("B4", "LONG"), hist, NOW)
    check("2 signaux déjà pris après une perte -> POST_SL_CAP",
          not dec.allowed and dec.code == POST_SL_CAP, dec.detail[:70])

    # 9. Cas sain : zone neuve, cooldowns respectés -> autorisé
    hist = [rec("C1", "LONG", YESTERDAY, 8, EXIT_TP, YESTERDAY, 10)]
    dec = eng.check(Candidate("C2", "SHORT"), hist, NOW)
    check("zone neuve + dernier TP clôturé hier -> autorisé",
          dec.allowed and dec.code == "", dec.detail)

    # 10. FrequencyMonitor : mesures §26
    hist = [
        rec("D1", "LONG", TODAY, 1, EXIT_TP, TODAY, 3),
        rec("D2", "SHORT", TODAY, 5, EXIT_SL, TODAY, 7),
        rec("D3", "LONG", TODAY, 9),
    ]
    stats = FrequencyMonitor.stats(hist, NOW)
    check("monitor : today=3, zones=3, TP=1, SL=1, open=1",
          stats["today"] == 3 and stats["zones_today"] == 3
          and stats["tp_count"] == 1 and stats["sl_count"] == 1
          and stats["open"] == 1, str(stats))

    # 11. Nouvelle structure = cassure ou zone créée aussi (§25 complet)
    ctx2 = ContextEvents(
        break_times=[pd.Timestamp(f"{TODAY} 10:00", tz="UTC")])
    dec = eng.check(Candidate("OB-2", "SHORT", "A"),
                    [rec("OB-2", "LONG", TODAY, 2, EXIT_SL, TODAY, 6)],
                    NOW, ctx2)
    check("nouvelle CASSURE après la sortie -> re-entry autorisé aussi",
          dec.allowed)

    # 12. L'ordre des garde-fous : le dedup prime sur tout (§4 esprit)
    hist = [rec("E1", "LONG", TODAY, 2)]      # en cours, même zone
    dec = eng.check(Candidate("E1", "LONG"), hist, NOW)
    check("signal en cours sur la même identité -> DUPLICATE (priorité au dedup)",
          not dec.allowed and dec.code == DUPLICATE_SETUP)

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — Anti-Overtrading Engine validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
