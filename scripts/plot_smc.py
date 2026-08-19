"""Génère le graphique interactif des zones SMC.

Usage :
    python scripts/plot_smc.py                 # EURUSD 15m par défaut
    python scripts/plot_smc.py XAUUSD 15m
    python scripts/plot_smc.py EURUSD 1h
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.smc import SMCEngine
from src.data.data_fetcher import DataFetcher
from src.logger import setup_logging
from src.visualization.smc_chart import plot_smc


def main() -> int:
    pair = sys.argv[1].upper() if len(sys.argv) > 1 else "EURUSD"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"

    setup_logging(level="INFO")
    fetcher = DataFetcher()
    df = fetcher.get_candles(pair, tf, lookback_days=30)

    engine = SMCEngine(pair, tf)
    result = engine.analyze(df)

    out = Path("data/charts") / f"smc_{pair}_{engine.timeframe.value}.html"
    path = plot_smc(result, df, out)

    n_ob = len(result["events"]["order_blocks"])
    n_fvg = len(result["events"]["fair_value_gaps"])
    n_str = len(result["events"]["structure"])
    n_sw = len(result["events"]["liquidity"]["sweeps"])
    print(f"Graphique généré : {path}")
    print(f"  {n_str} événements structure (BOS/CHoCH) | {n_ob} OB | {n_fvg} FVG | {n_sw} sweeps")
    print(f"  Tendance {engine.timeframe.value} : {result['trend']['state']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
