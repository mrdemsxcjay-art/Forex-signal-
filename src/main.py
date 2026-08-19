"""Boucle temps réel du moteur de signaux — Phase 6.1.

Usage :
    python -m src.main               # boucle continue (Ctrl+C = arrêt propre)
    python -m src.main --once        # un seul cycle (test / cron / diagnostic)
    python -m src.main --interval 60 # override de la cadence (secondes)

Engagements du comité de pilotage implémentés ici :
    - isolation d'erreurs PAR PAIRE : une source en échec n'arrête rien ;
    - fraîcheur des données journalisée à chaque cycle (réserve Agent 2) ;
    - TTL du calendrier économique géré par l'analyseur (réserve Agent 1) ;
    - tracker de clôture à chaque cycle : aucun EN_COURS de plus de 48
      bougies (réserve Agent 3) ;
    - heartbeat Telegram au démarrage/arrêt, alerte de rétablissement
      après >= 2 échecs consécutifs d'une paire ;
    - arrêt propre sur SIGINT/SIGTERM avec statistiques de session.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal as sys_signal
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from .config import load_config
from .data.data_fetcher import DataFetcher
from .logger import setup_logging
from .notifications.telegram import TelegramSender, format_heartbeat
from .signals.engine import SignalEngine
from .signals.tracker import SignalTracker
from .storage.database import SignalDatabase

logger = logging.getLogger(__name__)


def sleep_interruptible(seconds: float, stop: threading.Event) -> None:
    """Sommeil par tranches : réagit immédiatement à une demande d'arrêt."""
    end = time.monotonic() + max(0.0, seconds)
    while not stop.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


class LoopRunner:
    """Orchestre cycles moteur + clôtures + messages opérationnels."""

    VERSION = "1.0.0"

    def __init__(self, cfg=None, engine: SignalEngine | None = None,
                 tracker: SignalTracker | None = None) -> None:
        self.cfg = cfg or load_config()
        self.fetcher = DataFetcher(config=self.cfg)
        self.db = SignalDatabase("data/signals.db")
        self.engine = engine or SignalEngine(
            config=self.cfg, fetcher=self.fetcher, database=self.db,
            telegram=TelegramSender.from_config(self.cfg),
        )
        self.telegram = self.engine.telegram
        self.tracker = tracker or SignalTracker(
            self.db, self.fetcher, telegram=self.telegram,
            expiry_bars=48,
        )
        self.pairs = list(self.cfg.trading.pairs)
        self.interval = int(self.cfg.trading.poll_interval_seconds)
        self.stop = threading.Event()
        self.failures: dict[str, int] = {p: 0 for p in self.pairs}
        self.n_cycles = 0
        self.n_signals = 0
        self.n_resolutions = 0
        self.n_errors = 0
        self.started_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    def run_cycle(self) -> dict:
        """Un cycle complet : toutes les paires (isolées) + clôtures."""
        self.n_cycles += 1
        summary: dict = {"cycle": self.n_cycles, "pairs": {}, "resolutions": []}

        for pair in self.pairs:
            try:
                report = self.engine.run_pair(pair)
                was_failing = self.failures.get(pair, 0) >= 2
                self.failures[pair] = 0
                info = {
                    "aligned": report.aligned,
                    "score": report.score,
                    "signal": report.signal is not None,
                    "blockers": report.blockers[:2],
                    "breakdown": report.breakdown,
                    "timeframes": report.alignment_detail,
                }
                if report.signal is not None:
                    self.n_signals += 1
                    sig = report.signal
                    logger.info(
                        "[%s] SIGNAL %s %d/100 (%s) — entrée %s, TP %s, SL %s, id=%s",
                        pair, sig.direction, sig.score, sig.grade,
                        sig.risk.entry, sig.risk.tp, sig.risk.sl, sig.db_id,
                    )
                else:
                    logger.info(
                        "[%s] cycle ok — score %d, bloqué par : %s",
                        pair, report.score, info["blockers"] or "rien",
                    )
                if was_failing:
                    self._notify(f"[{pair}] source de données rétablie — analyse reprise.")
                summary["pairs"][pair] = info
            except Exception as exc:  # noqa: BLE001 — isolation par paire
                self.n_errors += 1
                self.failures[pair] = self.failures.get(pair, 0) + 1
                logger.error(
                    "[%s] cycle en échec (tentative %d, isolé) : %s",
                    pair, self.failures[pair], exc,
                )
                summary["pairs"][pair] = {"error": str(exc)}

        try:
            resolutions = self.tracker.update_all()
            summary["resolutions"] = resolutions
            self.n_resolutions += len(resolutions)
        except Exception as exc:  # noqa: BLE001
            self.n_errors += 1
            logger.error("[tracker] échec (isolé) : %s", exc)

        # Persistance du rapport de cycle (transparence dashboard, sans risque :
        # une écriture ratée ne perturbe jamais la boucle)
        try:
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cycle": self.n_cycles,
                "signals_total": self.n_signals,
                "resolutions_total": self.n_resolutions,
                "errors_total": self.n_errors,
                "pairs": summary["pairs"],
            }
            Path("data/last_cycle.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("last_cycle.json non écrit : %s", exc)

        return summary

    # ------------------------------------------------------------------ #
    def _notify(self, text: str) -> None:
        try:
            self.telegram.send_text(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notification impossible : %s", exc)

    def _uptime(self) -> str:
        delta = datetime.now(timezone.utc) - self.started_at
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        return f"{hours}h{rem // 60:02d}"

    def heartbeat_start(self) -> None:
        logger.info(
            "Moteur v%s démarré — paires %s, cycle %ds, seuil %s/100",
            self.VERSION, ", ".join(self.pairs), self.interval,
            self.engine.threshold,
        )
        self._notify(format_heartbeat(started=True, info={
            "version": self.VERSION, "pairs": self.pairs,
            "interval": self.interval, "threshold": self.engine.threshold,
        }))

    def heartbeat_stop(self) -> None:
        logger.info(
            "Arrêt propre — %d cycles, %d signaux, %d clôtures, %d erreurs isolées (%s)",
            self.n_cycles, self.n_signals, self.n_resolutions, self.n_errors, self._uptime(),
        )
        self._notify(format_heartbeat(started=False, info={
            "cycles": self.n_cycles, "signals": self.n_signals,
            "resolutions": self.n_resolutions, "errors": self.n_errors,
            "uptime": self._uptime(),
        }))

    # ------------------------------------------------------------------ #
    def run_forever(self) -> None:
        """Boucle principale avec arrêt propre sur SIGINT/SIGTERM."""
        def _handle(signum, frame):  # noqa: ARG001
            logger.info("Signal %s reçu — arrêt demandé", signum)
            self.stop.set()

        sys_signal.signal(sys_signal.SIGINT, _handle)
        sys_signal.signal(sys_signal.SIGTERM, _handle)

        self.heartbeat_start()
        try:
            while not self.stop.is_set():
                t0 = time.monotonic()
                summary = self.run_cycle()
                elapsed = time.monotonic() - t0
                logger.info(
                    "=== cycle %d terminé en %.1fs — %d signal(s) émis au total ===",
                    summary["cycle"], elapsed, self.n_signals,
                )
                sleep_interruptible(self.interval - elapsed if elapsed < self.interval
                                    else 1.0, self.stop)
        finally:
            self.heartbeat_stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Moteur de signaux Forex SMC (analyse uniquement)")
    parser.add_argument("--once", action="store_true", help="exécute un seul cycle puis sort")
    parser.add_argument("--interval", type=int, default=None,
                        help="cadence de la boucle en secondes (override config)")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(
        level=cfg.logging.level,
        log_file=cfg.log_file_path,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
    )
    runner = LoopRunner(cfg)
    if args.interval:
        runner.interval = args.interval
    if args.once:
        summary = runner.run_cycle()
        logger.info("Mode --once : cycle %d terminé.", summary["cycle"])
        # Confirmation visible (premier lancement manuel / workflow_dispatch) :
        # CONFIRM_ONCE=1 -> un message Telegram résume le cycle, preuve de vie.
        if os.getenv("CONFIRM_ONCE") == "1":
            runner._notify(
                "MOTEUR OPERATIONNEL\n"
                f"Cycle {summary['cycle']} exécute : "
                f"{', '.join(runner.pairs)}\n"
                "Prochains cycles : automatiques toutes les 5 min.\n"
                "Trading = risque. DYOR."
            )
        return 0
    runner.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
