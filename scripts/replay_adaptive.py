"""Replay causal + walk-forward du moteur adaptatif — PHASE 12 (CLI).

Usage :
    python scripts/replay_adaptive.py                      # replay 45 j, défaut
    python scripts/replay_adaptive.py --days 60 --grid    # + grille min_rr1 IS/OOS
    python scripts/replay_adaptive.py --stride 2          # échantillonnage plus léger

Le replay est TECHNIQUE PUR (news/DXY neutralisés — F-08 documenté) :
l'écart avec le live (bonus DXY +10 quand aligné) est connu et borné.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.analysis.adaptive_engine import EngineConfig
from src.backtest.adaptive_replay import run_replay, sample_instants, walk_forward
from src.data.data_fetcher import DataFetcher
from src.logger import setup_logging


def fmt_stats(s: dict) -> str:
    if not s["closed"]:
        return (f"{s['signals']} signal(s), 0 clôture")
    return (f"{s['signals']} sig · {s['closed']} clôturés "
            f"(TP {s['tp']}/SL {s['sl']}/EXP {s['expire']}) · "
            f"winrate {s['winrate']*100:.0f}% · exp {s['expectancy_r']:+.2f}R · "
            f"PF {s['profit_factor']} · streak {s['max_losing_streak']} · "
            f"MAE {s['avg_mae_r']}R / MFE {s['avg_mfe_r']}R")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay causal du moteur adaptatif")
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--baseline", type=int, default=24)
    parser.add_argument("--grid", action="store_true",
                        help="walk-forward : grille min_rr1 sur IS, finalistes sur OOS")
    parser.add_argument("--oos-frac", type=float, default=0.33)
    args = parser.parse_args()

    setup_logging(level="WARNING")
    print(f"Chargement des données ({args.days} jours de replay)...")
    f = DataFetcher()
    frames = {
        "D1": f.get_candles("EURUSD", "1d", lookback_days=400),
        "H4": f.get_candles("EURUSD", "4h", lookback_days=120),
        "H1": f.get_candles("EURUSD", "1h", lookback_days=60),
        "M30": f.get_candles("EURUSD", "30m", lookback_days=30),
        "M15": f.get_candles("EURUSD", "15m", lookback_days=args.days + 5),
        "M5": f.get_candles("EURUSD", "5m", lookback_days=30),
    }
    m15 = frames["M15"]
    start = m15.index[-1] - pd.Timedelta(days=args.days)
    instants = sample_instants(m15, start=start, stride=args.stride,
                               baseline_stride=args.baseline)
    print(f"{len(instants)} instants causaux "
          f"({instants[0]:%d %b} -> {instants[-1]:%d %b}, stride {args.stride})")

    if not args.grid:
        r = run_replay(frames, instants, EngineConfig(), "defaut")
        s = r.stats()
        print(f"\n=== REPLAY DÉFAUT ({args.days} j, {r.n_instants} instants, "
              f"{r.duration_s:.0f}s) ===")
        print(f"  {fmt_stats(s)}")
        total_no = sum(s["no_trade_codes"].values())
        print(f"\n  NO_TRADE ({total_no}) :")
        for code, n in list(s["no_trade_codes"].items())[:10]:
            print(f"    {code:<28} {n:5d} ({n/total_no*100:.0f} %)")
        return 0

    # ---- walk-forward avec grille min_rr1 (§30) ---------------------------
    candidates = {
        "rr1.2": EngineConfig(),
        "rr1.5": EngineConfig(),
        "rr2.0": EngineConfig(),
    }
    candidates["rr1.2"].risk = dict(candidates["rr1.2"].risk, min_rr1=1.2)
    candidates["rr2.0"].risk = dict(candidates["rr2.0"].risk, min_rr1=2.0)

    wf = walk_forward(frames, instants, candidates, oos_frac=args.oos_frac)
    print(f"\n=== WALK-FORWARD (IS {wf['n_is']} instants / OOS {wf['n_oos']}) ===")
    print(f"{'config':<8} {'IN-SAMPLE':<68}")
    for label, s in wf["is"].items():
        print(f"{label:<8} {fmt_stats(s)}")
    print(f"\n{'config':<8} {'OUT-OF-SAMPLE':<68}")
    for label, s in wf["oos"].items():
        print(f"{label:<8} {fmt_stats(s)}")
    print("\n  §30 : le paramètre retenu doit être robuste IS -> OOS, pas juste")
    print("  le meilleur in-sample. Aucun réglage manuel sur quelques trades.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
