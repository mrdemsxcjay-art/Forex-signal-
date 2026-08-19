"""Backtest SIMPLE et honnête du moteur SMC (sans look-ahead).

STRATÉGIE TESTÉE (CHoCH reversal, style ICT) :
    1. Le moteur SMC analyse TOUT l'historique une seule fois (légitime car
       anti-repaint : les événements passés ne dépendent pas du futur) ;
    2. Pour chaque CHoCH confirmé à la bougie i :
         entrée  = OPEN de la bougie i+1 (jamais la clôture de i -> pas de
                   remplissage rétroactif) ;
         stop    = bas (OB lié si présent, sinon plus bas des 5 bougies avant
                   la cassure) − sl_buffer_atr × ATR ;
         objectif = entrée ± rr × risque (défaut 2R) ;
    3. Déroulement bougie par bougie :
         si TP et SL sont touchés dans la MÊME bougie -> perte (conservateur) ;
         timeout après `timeout_bars` -> sortie à clôture (R partiel) ;
    4. Une position à la fois (réaliste pour du suivi manuel de signaux).

LIMITES ASSUMÉES (backtest « simple » comme demandé) :
    - spread/coûts optionnels (paramètre `spread`, 0 par défaut) appliqués
      à l'entrée ; pas de slippage, pas de gap de week-end géré finement ;
    - l'entrée suppose un ordre au marché au prix d'ouverture -> signal
      temps réel exécutable par l'utilisateur (rappel : l'app n'exécute rien).

Sorties : stats (winrate, R moyen, profit factor, drawdown max) + trades CSV.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BacktestConfig:
    rr: float = 2.0                 # take-profit en multiples du risque
    timeout_bars: int = 48          # sortie forcée (48 bougies M15 = 12 h)
    sl_buffer_atr: float = 0.10     # marge du stop sous/sus la zone
    spread: float = 0.0             # coût d'entrée en prix (ex: 0.0001 EURUSD)
    use_events: tuple = ("CHoCH",)  # types d'événements tradés


class SMCBacktester:
    """Rejoue la stratégie CHoCH sur le résultat d'une analyse SMCEngine."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.cfg = config or BacktestConfig()

    def run(self, df: pd.DataFrame, smc_result: dict) -> tuple[dict, pd.DataFrame]:
        """Args:
            df: bougies clôturées (format standard projet) utilisées pour l'analyse.
            smc_result: dict renvoyé par SMCEngine.analyze(df) MÊME paire/timeframe.
        """
        cfg = self.cfg
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        times = df.index
        n = len(df)

        # ATR approximé depuis le résultat (dernière valeur) pour le buffer SL
        atr_last = float(smc_result["atr"]) or 1e-9

        obs_by_event = {ob["break_event_id"]: ob for ob in smc_result["events"]["order_blocks"]}
        events = [
            e for e in smc_result["events"]["structure"]
            if e["type"] in cfg.use_events
        ]

        trades: list[dict] = []
        busy_until = -1

        for ev in events:
            i = ev["break_index"]
            entry_idx = i + 1
            if entry_idx >= n:  # événement trop récent : pas encore exécutable
                continue
            if entry_idx <= busy_until:  # déjà en position
                continue

            bullish = ev["direction"] == "bullish"
            entry = float(opens[entry_idx])
            if bullish:
                entry += cfg.spread
            else:
                entry -= cfg.spread

            # Stop : zone OB liée si disponible, sinon extremum local
            ob = obs_by_event.get(ev["id"])
            if ob is not None:
                zone = float(ob["zone_bottom"] if bullish else ob["zone_top"])
            else:
                lo = max(0, i - 5)
                zone = float(np.min(lows[lo:i + 1]) if bullish else np.max(highs[lo:i + 1]))

            if bullish:
                sl = zone - cfg.sl_buffer_atr * atr_last
                risk = entry - sl
                tp = entry + cfg.rr * risk
            else:
                sl = zone + cfg.sl_buffer_atr * atr_last
                risk = sl - entry
                tp = entry - cfg.rr * risk
            if risk <= 0:  # zone incohérente avec le prix -> on ignore
                continue

            # Déroulement
            exit_price, exit_reason, exit_idx = None, None, None
            last = min(n - 1, entry_idx + cfg.timeout_bars)
            for j in range(entry_idx, last + 1):
                if bullish:
                    if lows[j] <= sl:  # SL testé en premier = conservateur
                        exit_price, exit_reason, exit_idx = sl, "SL", j
                        break
                    if highs[j] >= tp:
                        exit_price, exit_reason, exit_idx = tp, "TP", j
                        break
                else:
                    if highs[j] >= sl:
                        exit_price, exit_reason, exit_idx = sl, "SL", j
                        break
                    if lows[j] <= tp:
                        exit_price, exit_reason, exit_idx = tp, "TP", j
                        break
            if exit_price is None:
                exit_price, exit_reason, exit_idx = float(closes[last]), "timeout", last

            r_multiple = ((exit_price - entry) / risk) if bullish else ((entry - exit_price) / risk)
            trades.append({
                "event_id": ev["id"],
                "event_type": ev["type"],
                "direction": ev["direction"],
                "entry_time": times[entry_idx],
                "entry": round(entry, 6),
                "sl": round(sl, 6),
                "tp": round(tp, 6),
                "exit_time": times[exit_idx],
                "exit": round(exit_price, 6),
                "reason": exit_reason,
                "bars_held": exit_idx - entry_idx,
                "r": round(float(r_multiple), 3),
            })
            busy_until = exit_idx

        return self._stats(trades), pd.DataFrame(trades)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _stats(trades: list[dict]) -> dict:
        if not trades:
            return {
                "trades": 0, "wins": 0, "losses": 0, "timeouts": 0,
                "winrate": None, "avg_r": None, "total_r": 0.0,
                "profit_factor": None, "max_drawdown_r": 0.0,
            }
        rs = np.array([t["r"] for t in trades])
        wins = int((rs > 0).sum())
        losses = int((rs < 0).sum())
        timeouts = int(sum(1 for t in trades if t["reason"] == "timeout"))
        gross_win = float(rs[rs > 0].sum())
        gross_loss = float(-rs[rs < 0].sum())
        equity = np.cumsum(rs)
        peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))
        drawdown = float((peak - np.concatenate([[0.0], equity])).max())

        return {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "timeouts": timeouts,
            "winrate": round(wins / len(trades), 3),
            "avg_r": round(float(rs.mean()), 3),
            "total_r": round(float(rs.sum()), 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "max_drawdown_r": round(drawdown, 2),
        }
