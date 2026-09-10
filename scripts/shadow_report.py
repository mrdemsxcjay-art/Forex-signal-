"""Rapport Paper Shadow Mode — comparaison OLD (live) vs NEW (§27/§36).

Usage :  python scripts/shadow_report.py
Luit data/signals.db (moteur live) et data/shadow.db (moteur adaptatif
papier) et produit le tableau de comparaison demandé par la spec §27 :
signaux, TP/SL/EXPIRE, winrate, expectancy, profit factor, drawdown,
losing streak, MAE/MFE, et distribution des NO_TRADE (§33).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logger import setup_logging
from src.shadow.shadow_recorder import ShadowDatabase
from src.storage.database import SignalDatabase


def old_stats() -> dict | None:
    path = Path("data/signals.db")
    if not path.exists():
        return None
    return SignalDatabase(path).stats()


def main() -> int:
    setup_logging(level="ERROR")
    print("=" * 70)
    print(" RAPPORT SHADOW — ancien moteur (live) vs nouveau moteur (papier)")
    print("=" * 70)

    o = old_stats()
    if o:
        print("\n--- ANCIEN MOTEUR (live, signaux réels) ---")
        print(f"  signaux clôturés : {o['closed']} (TP {o['tp']} / SL {o['sl']} / "
              f"EXPIRE {o['expired']}, {o['open']} ouverts)")
        print(f"  winrate          : {o['winrate']}")
        print(f"  R total / moyen  : {o['total_r']:+.1f} / {o['avg_r']}")
    else:
        print("\n--- ANCIEN MOTEUR : base absente ---")

    shadow_path = Path("data/shadow.db")
    if not shadow_path.exists():
        print("\n--- NOUVEAU MOTEUR (papier) : base absente (premiers cycles en cours) ---")
        return 0
    s = ShadowDatabase(shadow_path).stats()

    print("\n--- NOUVEAU MOTEUR (papier, phases 2-10) ---")
    print(f"  cycles analysés  : {s['decisions']}")
    print(f"  signaux papier   : {s['signals']} "
          f"({s['signals'] / max(s['decisions'], 1) * 100:.1f} % des cycles)")
    if s["closed"]:
        print(f"  clôtures papier  : {s['closed']} (TP {s['tp']} / SL {s['sl']} / "
              f"EXPIRE {s['expire']})")
        print(f"  winrate          : {s['winrate']}")
        print(f"  expectancy       : {s['expectancy_r']:+.2f}R par signal")
        print(f"  R total          : {s['total_r']:+.1f}")
        print(f"  profit factor    : {s['profit_factor']}")
        print(f"  série perdante max : {s['max_losing_streak']}")
        print(f"  MAE moyen        : {s['avg_mae_r']}R (pire excursion adverse)")
        print(f"  MFE moyen        : {s['avg_mfe_r']}R (meilleure favorable)")
        print(f"  durée moyenne    : {s['avg_time_to_exit_min']} min")
    else:
        print("  aucune clôture papier à ce jour")

    print("\n--- DISTRIBUTION DES NO_TRADE (§33 : le filtre est-il trop strict ?) ---")
    total_no = sum(s["no_trade_codes"].values())
    if total_no:
        for code, n in list(s["no_trade_codes"].items())[:10]:
            print(f"  {code:<28} {n:5d}  ({n / total_no * 100:.0f} %)")
    else:
        print("  aucun NO_TRADE journalisé")

    print("\n  Note : le nouveau moteur doit accumuler des décisions sur plusieurs")
    print("  semaines avant toute conclusion statistique (§36). Le basculement")
    print("  éventuel se fera seulement après validation hors-échantillon (P12).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
