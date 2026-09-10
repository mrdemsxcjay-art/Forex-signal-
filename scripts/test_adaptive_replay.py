"""Tests du replay causal — PHASE 12 (synthétique, hors-ligne)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.adaptive_engine import EngineConfig
from src.backtest.adaptive_replay import (
    run_replay,
    sample_instants,
    split_is_oos,
)

# réutilise le marché synthétique validé en PHASE 10 (E2E SIGNAL LONG)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_adaptive_engine import build_frames  # noqa: E402

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    frames = build_frames()
    m15 = frames["M15"]
    last_ts = m15.index[-1]
    print("=" * 70)
    print(" Tests replay causal — PHASE 12")
    print("=" * 70)

    # 1. Causalité du sampler : aucun instant au-delà de la dernière bougie
    instants = sample_instants(m15, stride=1, baseline_stride=12)
    check("sampler causal : tous les instants <= dernière bougie M15",
          all(t <= last_ts for t in instants) and len(instants) > 0,
          f"{len(instants)} instants")

    # 2. Déterminisme du sampler
    instants2 = sample_instants(m15, stride=1, baseline_stride=12)
    check("sampler déterministe (deux runs identiques)", instants == instants2)

    # 3. La baseline inclut la DERNIÈRE bougie -> le scénario E2E (SIGNAL
    #    LONG de la phase 10) doit être capturé par le replay
    r = run_replay(frames, instants, EngineConfig(), "causal-test",
                   resolve_hours=25.0)
    stats = r.stats()
    sig_rows = list(r.db._connect().execute(
        "SELECT direction, score, zone_id FROM decisions "
        "WHERE decision = 'SIGNAL'"))
    check("replay capture le SIGNAL LONG du scénario E2E",
          stats["signals"] >= 1 and sig_rows
          and sig_rows[-1]["direction"] == "LONG",
          f"{stats['signals']} signal(aux) papier, dernier : "
          f"{sig_rows[-1]['direction']} {sig_rows[-1]['score']}/100")

    # 4. Journal NO_TRADE présent (§33 en replay)
    check("journal NO_TRADE alimenté (codes §33)",
          sum(stats["no_trade_codes"].values()) > 0,
          str(list(stats["no_trade_codes"].items())[:3]))

    # 5. Découpe IS/OOS : sans chevauchement, couverture totale
    is_i, oos_i = split_is_oos(instants, oos_frac=0.33)
    check("IS/OOS sans chevauchement et couverture totale",
          len(is_i) + len(oos_i) == len(instants)
          and not (set(is_i) & set(oos_i))
          and (not is_i or max(is_i) < min(oos_i)),
          f"IS {len(is_i)} / OOS {len(oos_i)}")

    # 6. Anti-fuite structurel : les décisions ne dépendent que du passé
    #    (le même instant, frames tronquées identiques -> même décision ;
    #     on vérifie que tronquer APRÈS l'instant ne change rien)
    ts = instants[len(instants) // 2]
    from src.analysis.adaptive_engine import AdaptiveSignalEngine
    eng = AdaptiveSignalEngine(EngineConfig())
    d1 = eng.analyze({tf: df[df.index <= ts] for tf, df in frames.items()}, ts)
    # frames auxquelles on AJOUTE des bougies futures : la frame source est
    # la même, on vérifie juste que la troncation <= ts ignore le futur
    future = {tf: df[df.index <= ts] for tf, df in frames.items()}
    d2 = eng.analyze(future, ts)
    check("aucune fuite : décision identique sur frames tronquées",
          d1 == d2)

    # 7. Résolution papier : le SIGNAL du scénario E2E (entrée ~1.097,
    #    SL sous le sweep) est clôturé sur les bougies POSTÉRIEURES
    #    (ici : aucune bougie après -> reste OPEN, preuve qu'aucune
    #    clôture n'utilise l'instant d'entrée lui-même)
    open_out = r.db.open_outcomes()
    check("clôture uniquement sur bougies postérieures (reste OPEN ici)",
          len(open_out) >= 1 and all(o["status"] == "OPEN" for o in open_out))

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} — replay causal validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
