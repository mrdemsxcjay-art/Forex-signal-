"""Replay historique du moteur de signaux COMPLET — backtest d'intégration.

Différence avec src/backtest/backtester.py (backtest SMC simple, événements
CHoCH seulement) : ici on rejoue le VRAI moteur multi-agents, portes incluses —

    alignement D1/H4/M15 -> scoring /100 -> seuil -> agent risque (news,
    R/R) -> anti-spam/cooldown -> puis clôture par la logique du tracker.

Méthode :
    1. bougies D1/H4/M15 chargées UNE fois par paire (30 jours) ;
    2. instants d'échantillonnage = clôtures M15 suivant chaque événement de
       structure (le déclencheur vit au maximum 6 bougies ; on teste à +1
       et +3 pour couvrir les retests) — pas de balayage aveugle ;
    3. à chaque instant : troncature chronologique stricte (aucune fuite
       du futur), run_on_frames(now=instant) sur une base temporaire ;
    4. clôture des signaux émis via SignalTracker sur les bougies réelles.

Limite honnête : le calendrier ForexFactory couvre la semaine courante ->
la composante FONDAMENTALE est neutre sur l'historique (pas de +25).
C'est un backtest TECHNIQUE : les signaux doivent être quasi parfaits
(≥ 70 sans les news), d'où un débit volontairement faible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.agents.fundamental_agent import FundamentalAgent
from src.analysis.smc import SMCEngine
from src.data.data_fetcher import DataFetcher
from src.data.provider import Timeframe
from src.notifications.telegram import TelegramSender
from src.signals.engine import SignalEngine
from src.signals.tracker import SignalTracker
from src.storage.database import SignalDatabase

logger = logging.getLogger(__name__)

TIMEFRAMES = (Timeframe.D1, Timeframe.H4, Timeframe.M15)


class _FrameStubFetcher:
    """Fetcher factice : renvoie la frame M15 complète d'une paire (tracker)."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def get_candles(self, pair, timeframe, lookback_days=30, only_closed=True):
        return self.frames[pair.upper()]


@dataclass
class ReplayResult:
    """Synthèse du replay (comparables entre seuils)."""

    label: str
    days: int
    threshold: float
    instants: int = 0
    signals: int = 0
    stats: dict = field(default_factory=dict)
    trades: pd.DataFrame = None  # type: ignore[assignment]
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    max_drawdown_r: float = 0.0


def collect_instants(m15: pd.DataFrame, days: int, offsets=(1, 3)) -> list[pd.Timestamp]:
    """Instants à rejouer : clôtures suivant chaque événement de structure M15."""
    events = SMCEngine("replay", "15m").analyze(m15)["events"]["structure"]
    cutoff = m15.index[-1] - pd.Timedelta(days=days)
    instants: set[pd.Timestamp] = set()
    for event in events:
        if pd.Timestamp(event["break_time"]) < cutoff:
            continue
        for offset in offsets:
            idx = event["break_index"] + offset
            if idx < len(m15):
                instants.add(m15.index[idx])
    return sorted(instants)


def replay(frames_by_pair: dict[str, dict[Timeframe, pd.DataFrame]],
           days: int = 30, threshold: float = 70.0,
           fundamental: FundamentalAgent | None = None,
           label: str = "replay", min_rr: float | None = None,
           default_rr: float | None = None, expiry_bars: int | None = None) -> ReplayResult:
    """Rejoue le moteur complet sur l'historique, base temporaire isolée."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="replay_")
    db = SignalDatabase(Path(tmp) / "replay.db")
    engine = SignalEngine(
        config=None, database=db,
        fundamental=fundamental or FundamentalAgent(),
        telegram=TelegramSender("", "", enabled=False),
    )
    engine.threshold = float(threshold)
    if min_rr is not None:
        engine.risk_agent.min_rr = float(min_rr)
    if default_rr is not None:
        engine.risk_agent.default_rr = float(default_rr)

    m15_frames = {p: frames[Timeframe.M15] for p, frames in frames_by_pair.items()}
    total_instants = 0
    n_signals = 0
    for pair, frames in frames_by_pair.items():
        d1, h4, m15 = frames[Timeframe.D1], frames[Timeframe.H4], frames[Timeframe.M15]
        instants = collect_instants(m15, days=days)
        total_instants += len(instants)
        for ts in instants:
            report = engine.run_on_frames(
                pair,
                d1[d1.index <= ts], h4[h4.index <= ts], m15[m15.index <= ts],
                now=ts,
            )
            if report.signal is not None:
                n_signals += 1
                logger.info("[replay] %s SIGNAL %s %d/100 @ %s",
                            label, pair, report.score, ts)

    # Clôture des signaux émis sur les bougies réelles
    tracker = SignalTracker(db, _FrameStubFetcher(m15_frames),
                            expiry_bars=expiry_bars or 48)
    tracker.update_all(now=datetime.now(timezone.utc))

    stats = db.stats()
    trades = db.recent(limit=10_000)
    trades = trades[trades["resultat"] != "EN_COURS"].sort_values("id")
    equity = trades["exit_r"].astype(float).cumsum()
    drawdown = 0.0
    if not equity.empty:
        peak = equity.cummax()
        drawdown = float((peak - equity).max())

    return ReplayResult(
        label=label, days=days, threshold=threshold,
        instants=total_instants, signals=n_signals, stats=stats,
        trades=trades, equity_curve=equity, max_drawdown_r=round(drawdown, 2),
    )


def load_frames(pairs: list[str], lookback_days: int = 30,
                fetcher: DataFetcher | None = None) -> dict[str, dict[Timeframe, pd.DataFrame]]:
    """Charge D1/H4/M15 une fois par paire."""
    fetcher = fetcher or DataFetcher()
    out: dict[str, dict[Timeframe, pd.DataFrame]] = {}
    for pair in pairs:
        mtd = fetcher.get_multi_timeframe_data(pair, TIMEFRAMES)
        out[pair.upper()] = {tf: mtd.frames[tf] for tf in TIMEFRAMES}
    return out
