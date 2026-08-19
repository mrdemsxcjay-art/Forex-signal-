"""Lance un backtest simple de la stratégie SMC (CHoCH + zone OB).

Usage :
    python scripts/backtest_smc.py              # EURUSD 15m par défaut
    python scripts/backtest_smc.py XAUUSD 15m
    python scripts/backtest_smc.py EURUSD 1h
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.smc import SMCEngine
from src.backtest.backtester import BacktestConfig, SMCBacktester
from src.data.data_fetcher import DataFetcher
from src.logger import setup_logging


def main() -> int:
    pair = sys.argv[1].upper() if len(sys.argv) > 1 else "EURUSD"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"

    setup_logging(level="INFO")
    df = DataFetcher().get_candles(pair, tf, lookback_days=30)
    engine = SMCEngine(pair, tf)
    result = engine.analyze(df)

    backtester = SMCBacktester(BacktestConfig(rr=2.0, timeout_bars=48))
    stats, trades = backtester.run(df, result)

    print("=" * 62)
    print(f" Backtest SMC — {pair} {engine.timeframe.value} "
          f"({len(df)} bougies, ~{len(df) * 15 / 60 / 24:.0f} jours)")
    print("=" * 62)
    print(f"  Trades      : {stats['trades']}")
    print(f"  Gagnants    : {stats['wins']}   Perdants : {stats['losses']}   "
          f"Timeouts : {stats['timeouts']}")
    print(f"  Winrate     : {stats['winrate']}")
    print(f"  R total     : {stats['total_r']}   R moyen : {stats['avg_r']}")
    print(f"  Profit fact.: {stats['profit_factor']}")
    print(f"  Drawdown max: {stats['max_drawdown_r']} R")
    if not trades.empty:
        out_dir = Path("data/backtest")
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / f"{pair}_{engine.timeframe.value}_trades.csv"
        trades.to_csv(csv_path, index=False, encoding="utf-8")
        json_path = out_dir / f"{pair}_{engine.timeframe.value}_stats.json"
        json_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  Détail des trades : {csv_path}")
        print("\n  5 derniers trades :")
        print(trades.tail(5).to_string(index=False))
    else:
        print("  Aucun trade généré (pas de CHoCH sur la période).")
    print("\n  ⚠ Backtest simple : coûts par défaut nuls, exécution à l'ouverture")
    print("    suivante, TP+SL même bougie = perte (conservateur).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
