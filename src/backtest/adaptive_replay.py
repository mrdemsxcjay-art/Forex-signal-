"""Replay ÉVÉNEMENTIEL CAUSAL du moteur adaptatif — PHASE 12 (§29-§30).

Réparations d'audit intégrées :
    F-07 (biais de sélection) : les instants testés sont les temps de
         réaction aux événements de structure M15 CONFIRMÉS (propriété
         anti-repaint prouvée en phase 5 : un événement émis à la bougie i
         est identique dans l'analyse tronquée à i). Le sampler ne peut
         donc JAMAIS sélectionner grâce au futur — contrairement à
         l'ancien replay qui choisissait ses instants sur l'analyse de
         l'historique COMPLET.
    F-08 (replay ≠ live)      : le replay est explicitement TECHNIQUE PUR
         (news_hours_fn=None, DXY non injecté) — l'écart avec le live est
         documenté au lieu d'être silencieux.

Méthode :
    1. instants = {clôture suivant chaque cassure M15 confirmée}
       + baseline périodique (pour les stats NO_TRADE §33) ;
    2. à chaque instant : AdaptiveSignalEngine.analyze() sur frames
       TRONQUÉES (aucune fuite), anti-overtrading nourri par l'historique
       papier (les clôtures sont résolues au fil de l'eau) ;
    3. clôtures papier + MAE/MFE via ShadowTracker (bougies postérieures) ;
    4. walk-forward : découpe IS/OOS sans chevauchement, grille de
       candidats évaluée sur IS, finalistes sur OOS (§30).
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ..analysis.adaptive_engine import AdaptiveSignalEngine, EngineConfig
from ..analysis.smc import SMCEngine
from ..shadow.shadow_recorder import ShadowDatabase
from ..shadow.shadow_tracker import ShadowTracker

logger = logging.getLogger(__name__)


class _FrameFetcher:
    """Fetcher factice : renvoie la frame M15 complète (résolution papier)."""

    def __init__(self, m15: pd.DataFrame):
        self.m15 = m15

    def get_candles(self, pair, timeframe, lookback_days=30, only_closed=True):
        return self.m15


def sample_instants(m15: pd.DataFrame, *, start: pd.Timestamp | None = None,
                    stride: int = 1, baseline_stride: int = 24) -> list[pd.Timestamp]:
    """Instants CAUSAUX : réaction aux cassures confirmées + baseline.

    Convention d'instant (alignée sur le moteur live) : l'événement (cassure
    par CLÔTURE de la bougie i) est connaissable dès la clôture de i ; le
    live évalue juste après -> instant = index[i], l'analyse recevant les
    bougies <= index[i] (la cassure est incluse, close-based donc finale).
    La preuve anti-repaint garantit que cet événement serait identique dans
    une analyse tronquée à cet instant (aucune fuite, F-07 réparé).
    """
    events = SMCEngine("REPLAY", "15m").analyze(m15)["events"]["structure"]
    instants: set[pd.Timestamp] = set()
    kept = 0
    for ev in sorted(events, key=lambda e: e["break_index"]):
        if start is not None and pd.Timestamp(ev["break_time"]) < start:
            continue
        if kept % max(stride, 1) == 0:
            idx = ev["break_index"]
            if idx < len(m15):
                instants.add(m15.index[idx])
        kept += 1
    for i in range(baseline_stride - 1, len(m15), baseline_stride):
        ts = m15.index[i]
        if start is None or ts >= start:
            instants.add(ts)
    if len(m15):                      # la dernière bougie est toujours testée
        instants.add(m15.index[-1])
    return sorted(instants)


def split_is_oos(instants: list[pd.Timestamp],
                 oos_frac: float = 0.33) -> tuple[list, list]:
    """Découpe temporelle sans chevauchement (walk-forward, §29)."""
    n_oos = max(1, int(len(instants) * oos_frac)) if instants else 0
    return instants[:-n_oos] if n_oos else list(instants), instants[-n_oos:]


@dataclass
class ReplayResult:
    label: str
    db: ShadowDatabase
    n_instants: int
    duration_s: float

    def stats(self) -> dict:
        return self.db.stats()


def run_replay(frames: dict[str, pd.DataFrame], instants: list[pd.Timestamp],
               config: EngineConfig | None = None, label: str = "replay",
               resolve_hours: float = 25.0) -> ReplayResult:
    """Rejoue le moteur adaptatif sur chaque instant (frames tronquées)."""
    import time as _time

    engine = AdaptiveSignalEngine(config or EngineConfig())
    m15 = frames["M15"]
    tmp = tempfile.mkdtemp(prefix="replay12_")
    db = ShadowDatabase(Path(tmp) / "shadow.db")
    tracker = ShadowTracker(db, _FrameFetcher(m15),
                            expiry_hours=resolve_hours)
    t0 = _time.monotonic()
    n_signal = 0
    for k, ts in enumerate(instants):
        trunc = {tf: df[df.index <= ts] for tf, df in frames.items()}
        decision = engine.analyze(trunc, ts, history=db.history_records())
        db.record_decision(decision, ts)
        if decision.decision == "SIGNAL":
            n_signal += 1
            # clôture au fil de l'eau (l'anti-overtrading des instants
            # suivants doit voir les sorties déjà réalisées)
            tracker.update_all(ts + pd.Timedelta(hours=resolve_hours + 1))
        if (k + 1) % 50 == 0:
            el = _time.monotonic() - t0
            logger.info("[replay %s] %d/%d instants (%.1fs, %.2fs/instant, "
                        "%d signaux)", label, k + 1, len(instants), el,
                        el / (k + 1), n_signal)
    duration = _time.monotonic() - t0
    logger.info("[replay %s] TERMINÉ : %d instants, %d signaux papier, %.0fs",
                label, len(instants), n_signal, duration)
    return ReplayResult(label, db, len(instants), duration)


def walk_forward(frames: dict[str, pd.DataFrame], instants: list[pd.Timestamp],
                 candidates: dict[str, EngineConfig], oos_frac: float = 0.33,
                 resolve_hours: float = 25.0) -> dict:
    """Grille sur l'in-sample, finalistes sur l'out-of-sample (§30).

    Returns: {"is": {label: stats}, "oos": {label: stats}, "instants": ...}
    """
    is_inst, oos_inst = split_is_oos(instants, oos_frac)
    out = {"n_is": len(is_inst), "n_oos": len(oos_inst), "is": {}, "oos": {}}
    for label, cfg in candidates.items():
        r = run_replay(frames, is_inst, cfg, f"IS/{label}", resolve_hours)
        out["is"][label] = r.stats()
    # finalistes = tous les candidats évalués sur OOS (le choix final se
    # fait sur la robustesse IS->OOS, jamais sur IS seul)
    for label, cfg in candidates.items():
        r = run_replay(frames, oos_inst, cfg, f"OOS/{label}", resolve_hours)
        out["oos"][label] = r.stats()
    return out
