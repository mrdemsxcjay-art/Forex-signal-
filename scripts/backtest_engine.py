"""Backtest étendu : replay du moteur complet + comparaison de seuils.

Usage :
    python scripts/backtest_engine.py                     # 30 j, seuil 70
    python scripts/backtest_engine.py --days 20 --compare # 70 vs 60
    python scripts/backtest_engine.py --pairs EURUSD,XAUUSD --threshold 65
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine_replay import load_frames, replay
from src.logger import setup_logging


def print_result(r) -> None:
    s = r.stats
    print(f"\n--- Replay « {r.label} » : seuil {r.threshold:.0f}/100, "
          f"{r.days} jours, {r.instants} instants rejoués ---")
    if r.signals == 0:
        print("  Aucun signal émis (moteur très sélectif sans fondamental historique).")
        return
    print(f"  Signaux émis        : {r.signals}")
    print(f"  Clôturés            : {s['closed']} "
          f"(TP {s['tp']} / SL {s['sl']} / EXPIRE {s['expired']}, "
          f"{s['open']} encore ouverts à la fin)")
    if s["closed"]:
        print(f"  Winrate             : {s['winrate'] * 100:.1f} %")
        print(f"  R total / moyen     : {s['total_r']:+.2f}R / {s['avg_r']:+.2f}R")
        print(f"  Drawdown max        : {r.max_drawdown_r:.2f}R")
        by_pair = r.trades.groupby("paire")["exit_r"].agg(["count", "sum"])
        for pair, row in by_pair.iterrows():
            print(f"    {pair:<7} {int(row['count'])} clôture(s), {row['sum']:+.2f}R")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest par replay du moteur complet")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--pairs", type=str, default="EURUSD,GBPUSD,XAUUSD")
    parser.add_argument("--threshold", type=float, default=70.0)
    parser.add_argument("--compare", action="store_true",
                        help="compare le seuil demandé au seuil 60")
    args = parser.parse_args()

    setup_logging(level="WARNING")
    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    print(f"Chargement D1/H4/M15 pour {pairs} ({args.days} jours)...")
    frames = load_frames(pairs)

    results = [replay(frames, days=args.days, threshold=args.threshold,
                      label=f"seuil {args.threshold:.0f}")]
    if args.compare:
        results.append(replay(frames, days=args.days, threshold=60.0, label="seuil 60"))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out_dir = Path("data/backtest")
    out_dir.mkdir(parents=True, exist_ok=True)

    for r in results:
        print_result(r)
        if r.trades is not None and not r.trades.empty:
            csv = out_dir / f"engine_replay_{r.label.replace(' ', '_')}_{stamp}.csv"
            r.trades.to_csv(csv, index=False, encoding="utf-8")
            print(f"  Trades détaillés    : {csv}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days, "pairs": pairs,
        "results": [{ "label": r.label, "threshold": r.threshold,
                      "instants": r.instants, "signals": r.signals,
                      "stats": r.stats, "max_drawdown_r": r.max_drawdown_r }
                    for r in results],
    }
    stats_path = out_dir / f"engine_replay_{stamp}.json"
    stats_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSynthèse JSON : {stats_path}")
    print("\nNote : replay TECHNIQUE (calendrier ForexFactory limité à la semaine "
          "courante -> composante fondamental neutre sur l'historique).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
