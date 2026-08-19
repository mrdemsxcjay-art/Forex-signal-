"""Diagnostic de gouvernance : chiffres réels pour la réunion des 3 agents.

Usage : python scripts/diagnostic.py

Produit les métriques dont les agents ont besoin pour décider de la
prochaine étape :
  1. Déclencheurs M15 réels (CHoCH/BOS) sur 14 jours, par paire
  2. Taux d'alignement D1 / H4 / M15 sur ces déclencheurs (les 3 portes)
  3. Taux de déclencheurs en session London/NY
  4. Cycle moteur réel à l'instant (bloqueurs courants)
  5. Posture opérationnelle : DB, Telegram, boucle continue, suivi TP/SL
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.smc import SMCEngine
from src.data.data_fetcher import DataFetcher
from src.data.provider import Timeframe
from src.logger import setup_logging
from src.signals.scoring import get_session


def d1_trend_timeline(structure_events):
    """Reconstruit la tendance D1 pas-à-pas : (break_time, trend)."""
    timeline, trend = [], None
    for e in structure_events:
        trend = e["direction"]  # toute cassure fixe la tendance (machine à états)
        timeline.append((pd.Timestamp(e["break_time"]), trend))
    return timeline


def trend_at(timeline, ts):
    trend = None
    for time, t in timeline:
        if time <= ts:
            trend = t
        else:
            break
    return trend


def main() -> None:
    setup_logging(level="ERROR")
    fetcher = DataFetcher()
    now = pd.Timestamp.now(tz="UTC")
    horizon = now - pd.Timedelta(days=14)

    print("=" * 66)
    print(f" DIAGNOSTIC SYSTÈME — {now:%Y-%m-%d %H:%M} UTC (fenêtre 14 jours)")
    print("=" * 66)

    totals = {"triggers": 0, "d1_ok": 0, "h4_ok": 0, "aligned3": 0, "session_ok": 0,
              "aligned3_session": 0}
    for pair in ("EURUSD", "GBPUSD", "XAUUSD"):
        mtd = fetcher.get_multi_timeframe_data(pair, ("1d", "4h", "15m"))
        d1, h4, m15 = mtd.frames[Timeframe.D1], mtd.frames[Timeframe.H4], mtd.frames[Timeframe.M15]
        r_d1 = SMCEngine(pair, "1d").analyze(d1)
        r_h4 = SMCEngine(pair, "4h").analyze(h4)
        r_m15 = SMCEngine(pair, "15m").analyze(m15)

        tl = d1_trend_timeline(r_d1["events"]["structure"])
        h4_events = [(pd.Timestamp(e["break_time"]), e["direction"])
                     for e in r_h4["events"]["structure"]]

        triggers = [e for e in r_m15["events"]["structure"]
                    if pd.Timestamp(e["break_time"]) >= horizon]
        n = d1_ok = h4_ok = a3 = sess = a3s = 0
        for e in triggers:
            ts = pd.Timestamp(e["break_time"])
            n += 1
            t_d1 = trend_at(tl, ts)
            recent_h4 = [d for t, d in h4_events if ts - pd.Timedelta(hours=24) <= t <= ts]
            ok_d1 = t_d1 == e["direction"]
            ok_h4 = bool(recent_h4) and recent_h4[-1] == e["direction"]
            ok_s = get_session(ts)[1]
            d1_ok += ok_d1
            h4_ok += ok_h4
            a3 += ok_d1 and ok_h4
            sess += ok_s
            a3s += ok_d1 and ok_h4 and ok_s

        for k, v in (("triggers", n), ("d1_ok", d1_ok), ("h4_ok", h4_ok),
                     ("aligned3", a3), ("session_ok", sess), ("aligned3_session", a3s)):
            totals[k] += v
        pct = lambda x: f"{(x / n * 100):5.1f} %" if n else "  n/a"
        print(f"\n{pair} :")
        print(f"  déclencheurs M15 (14 j)      : {n:4d}  ({n / 14:.1f}/jour)")
        print(f"  biais D1 aligné               : {pct(d1_ok)}  ({d1_ok})")
        print(f"  H4 récent aligné (24 h)       : {pct(h4_ok)}  ({h4_ok})")
        print(f"  TROIS timeframes alignés      : {pct(a3)}  ({a3})")
        print(f"  en session London/NY          : {pct(sess)}  ({sess})")
        print(f"  alignés ET en session         : {pct(a3s)}  ({a3s})")
        print(f"  tendance D1 actuelle          : {r_d1['trend']['state']}, "
              f"prix {r_m15['last_close']}")

    n = max(totals["triggers"], 1)
    print("\n" + "-" * 66)
    print(f"TOTAL 3 paires : {totals['triggers']} déclencheurs, "
          f"{totals['aligned3']} alignés ({totals['aligned3'] / n * 100:.1f} %), "
          f"{totals['aligned3_session']} alignés+session ({totals['aligned3_session'] / n * 100:.1f} %)")
    print(f"-> candidats/jour avant scoring : ~{totals['aligned3_session'] / 14:.1f} "
          "(puis scoring >=70, news, cooldown filtrent encore)")

    print("\nPOSTURE OPÉRATIONNELLE")
    db = Path("data/signals.db")
    print(f"  base signaux            : {'créée' if db.exists() else 'ABSENTE (aucun signal réel émis)'}")
    env = Path(".env")
    print(f"  Telegram configuré      : {'oui' if env.exists() else 'NON (.env absent)'}")
    print(f"  boucle continue main.py : ABSENTE (le moteur ne tourne qu'en tests)")
    print(f"  suivi TP/SL des signaux : ABSENT (resultat reste EN_COURS)")
    cards = list(Path("data/cards").glob("*.svg")) if Path("data/cards").exists() else []
    print(f"  cartes SVG produites    : {len(cards)} (tests uniquement)")


if __name__ == "__main__":
    main()
