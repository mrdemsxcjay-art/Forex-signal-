"""Moteur de signaux — orchestration des 3 agents (algorithme triple timeframe).

PIPELINE D'UN CYCLE (paire par paire) :

    1. DONNÉES   D1 + H4 + M15 clôturées (DataFetcher, fraîcheur vérifiée)
    2. AGENT 2   SMC multi-timeframe : biais D1, zones H4, déclencheur M15
    3. PORTE 1   ALIGNEMENT : biais D1 == H4 == déclencheur M15, sinon STOP
                 (« on n'envoie un signal QUE si les 3 timeframes sont alignés »)
    4. AGENT 1   fondamental : le sentiment soutient-il la direction ?
    5. SCORING   score /100 (fondamental 25, OB 20, FVG 15, CHoCH 15,
                 session 10, premium/discount 10, sweep 5)
    6. PORTE 2   score >= seuil (défaut 70), sinon STOP
    7. AGENT 3   risque : news rouge ? R/R >= 1.5 ? plan cohérent ?
    8. ANTI-SPAM cooldown par paire + quota quotidien
    9. ACTIONS   SQLite -> Telegram -> carte SVG (chacune indépendante et
                 non bloquante : une panne Telegram ne perd jamais le signal)

Chaque cycle rend un CycleReport complet (même sans signal) : le dashboard
pourra expliquer POURQUOI rien n'est parti.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..agents.eurusd_agent import GATE_TIMEFRAMES, EURUSDAgent
from ..agents.fundamental_agent import FundamentalAgent
from ..agents.risk_agent import RiskAgent
from ..agents.smc_agent import SMCAgent
from ..config import Config
from ..data.data_fetcher import DataFetcher
from ..data.provider import Timeframe
from ..notifications.telegram import TelegramSender
from ..storage.database import SignalDatabase
from ..visualization.signal_card import save_signal_card
from .models import CycleReport, Signal, utc_now_iso
from .scoring import compute_score, grade_of

logger = logging.getLogger(__name__)

DEFAULT_TIMEFRAMES = ("1d", "4h", "15m")


class SignalEngine:
    """Fait tourner le pipeline complet sur une paire (ou toutes)."""

    def __init__(
        self,
        config: Config | None = None,
        fetcher: DataFetcher | None = None,
        database: SignalDatabase | None = None,
        fundamental: FundamentalAgent | None = None,
        telegram: TelegramSender | None = None,
    ) -> None:
        self.cfg = config
        self.fetcher = fetcher or DataFetcher(config=config)
        self.db = database or SignalDatabase("data/signals.db")
        sig = _sig_params(config)
        self.smc_agent = SMCAgent(
            trigger_max_age=sig["trigger_max_age_candles"],
            zone_proximity_atr=sig["zone_proximity_atr"],
        )
        self.risk_agent = RiskAgent(
            min_rr=sig["min_rr"],
            default_rr=sig["default_rr"],
            risk_pct=sig["risk_pct"],
            account_size=sig["account_size"],
            news_block_minutes=config.news.block_minutes_before if config else 60,
        )
        self.fundamental = fundamental or FundamentalAgent()
        self.eurusd = EURUSDAgent()
        self.telegram = telegram or (
            TelegramSender.from_config(config) if config else TelegramSender("", "", enabled=False)
        )
        self.threshold = sig["min_score"]
        self.max_per_day = sig["max_per_pair_per_day"]
        self.cooldown = timedelta(minutes=sig["cooldown_minutes"])
        self.grades = sig["grades"]

    # ------------------------------------------------------------------ #
    #  Cycle complet sur des DataFrames (testable, déterministe)
    # ------------------------------------------------------------------ #
    def run_on_frames(
        self,
        pair: str,
        d1: pd.DataFrame,
        h4: pd.DataFrame,
        m15: pd.DataFrame,
        now: pd.Timestamp | None = None,
    ) -> CycleReport:
        now = now or pd.Timestamp.now(tz="UTC")
        report = CycleReport(
            pair=pair.upper(), generated_at=utc_now_iso(),
            candidate_direction=None, aligned=False, alignment_detail={},
            score=0, breakdown={}, passed_threshold=False, risk_valid=False, blockers=[],
        )

        # 2) Agent 2 : analyse SMC multi-timeframe
        view, analyses = self.smc_agent.analyze(pair, d1, h4, m15)
        report.candidate_direction = view.direction
        report.alignment_detail = {
            "D1": view.d1_bias,
            "H4": "soutient" if view.h4_supports else view.h4_reason,
            "M15": f"{view.m15_trigger_kind} {view.m15_trigger}" if view.m15_trigger else "aucun",
        }

        # 3) PORTE 1 — alignement des trois timeframes
        if view.direction is None:
            report.blockers.append("aucun déclencheur M15 récent")
            return report
        wanted = "bullish" if view.direction == "LONG" else "bearish"
        if view.d1_bias != wanted:
            report.blockers.append(f"biais D1 = {view.d1_bias}, requis = {wanted}")
            return report
        if not view.h4_supports:
            report.blockers.append(f"H4 non aligné : {view.h4_reason}")
            return report
        report.aligned = True

        # 4) Agent 1 : fondamental
        fundamental_view = self.fundamental.assess(pair, view.direction, now=now)

        # 5) Scoring /100
        session_label, _ = _session_for_display(now)
        score, breakdown, confluences = compute_score(
            view.direction, view, fundamental_view, now
        )
        report.score, report.breakdown = score, breakdown

        # 6) PORTE 2 — seuil
        report.passed_threshold = score >= self.threshold
        if not report.passed_threshold:
            report.blockers.append(f"score {score} < seuil {self.threshold}")
            return report

        # 7) Agent 3 : risque
        h4_liq = analyses["H4"]["events"]["liquidity"]
        plan = self.risk_agent.validate(
            pair, view.direction, view, m15,
            h4_liquidity=h4_liq, high_impact_soon=fundamental_view.high_impact_soon,
        )
        report.risk_valid = plan.valid
        if not plan.valid:
            report.blockers.extend(plan.blockers)
            return report

        # 8) Anti-spam
        blockers = self._spam_blockers(pair, now)
        if blockers:
            report.blockers.extend(blockers)
            return report

        # 9) Signal -> actions (horodaté à l'heure d'ANALYSE injectée :
        #    déterminisme complet des tests et cohérence du cooldown)
        signal = Signal(
            pair=pair.upper(), direction=view.direction, score=score,
            grade=grade_of(score, self.grades) or "B",
            session=session_label, risk=plan,
            confluences=confluences, breakdown=breakdown,
            timeframes=report.alignment_detail,
            fundamental={
                "bias": fundamental_view.bias, "score": fundamental_view.score,
                "supports": fundamental_view.supports_direction,
                "drivers": fundamental_view.drivers[:3],
            },
            created_at=now.strftime("%Y-%m-%d %H:%M:%S") + " UTC",
        )
        report.signal = signal
        self._dispatch(signal, report)
        return report

    # ------------------------------------------------------------------ #
    #  Cycle complet sur données réelles
    # ------------------------------------------------------------------ #
    def run_pair(self, pair: str, now: pd.Timestamp | None = None) -> CycleReport:
        now = now or pd.Timestamp.now(tz="UTC")
        # REGLE N°1 ABSOLUE : ce robot est exclusivement EUR/USD.
        if pair.upper() != "EURUSD":
            logger.warning("Paire '%s' ignoree : configure uniquement pour EUR/USD", pair)
            return CycleReport(
                pair=pair.upper(), generated_at=utc_now_iso(), candidate_direction=None,
                aligned=False, alignment_detail={}, score=0, breakdown={},
                passed_threshold=False, risk_valid=False,
                blockers=["configure uniquement pour EUR/USD"],
            )
        lookbacks = {Timeframe.D1: 400, Timeframe.H4: 90, Timeframe.H1: 60,
                     Timeframe.M30: 30, Timeframe.M15: 30, Timeframe.M5: 30}
        frames = {tf: self.fetcher.get_candles("EURUSD", tf, lookback_days=days)
                  for tf, days in lookbacks.items()}
        return self.run_eurusd(frames, now)

    def run_eurusd(self, frames: dict, now: pd.Timestamp) -> CycleReport:
        """Pipeline spec finale : 5 portes -> confiance -> news 2 h -> plan 1:3."""
        from ..fundamental.dxy import get_dxy
        from ..signals.models import FundamentalView, RiskPlan, pip_spec

        report = CycleReport(
            pair="EURUSD", generated_at=utc_now_iso(), candidate_direction=None,
            aligned=False, alignment_detail={}, score=0, breakdown={},
            passed_threshold=False, risk_valid=False, blockers=[],
        )
        try:
            dxy = get_dxy()
        except Exception:  # noqa: BLE001
            dxy = None

        # fondamental EUR/USD (Agent 1) + prochaine news HIGH EUR/USD
        bias = None
        try:
            bias = self.fundamental.analyzer.get_pair_bias("EURUSD", now=now)
            fund_view = FundamentalView(
                bias.label, bias.score, False,
                [f"{s.currency} {d}" for s in (bias.base, bias.quote) if s
                 for d in s.drivers[:2]],
                False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fondamental indisponible : %s", exc)
            fund_view = FundamentalView("NEUTRAL", 0.0, False, [], False)
        news_hours = None
        try:
            upcoming = self.fundamental.analyzer.next_high_impact_events(now=now)
            eur_usd = upcoming[upcoming["devise"].isin(["EUR", "USD"])] if not upcoming.empty else upcoming
            if not eur_usd.empty:
                news_hours = float(eur_usd.iloc[0]["dans_min"]) / 60.0
        except Exception:  # noqa: BLE001
            pass

        view = self.eurusd.assess(frames, dxy, fund_view, news_hours, now)
        report.candidate_direction = view.direction
        report.alignment_detail = view.gates
        report.blockers = list(view.blockers)
        if view.direction is None:
            return report
        report.aligned = True

        wanted = "BULLISH" if view.direction == "LONG" else "BEARISH"
        supports = bias is not None and bias.label == wanted and abs(bias.score) >= 1.2

        report.score = view.score
        report.breakdown = view.breakdown
        report.passed_threshold = view.score >= self.threshold
        if not report.passed_threshold:
            report.blockers.append(f"confiance {view.score}% < seuil {self.threshold:.0f}%")
            return report
        if news_hours is not None and news_hours < 2:
            report.blockers.append(f"news HIGH EUR/USD dans {news_hours:.1f} h (< 2 h) : signal bloque")
            return report

        pip_size, pip_value = pip_spec("EURUSD")
        risk_abs = abs(view.entry - view.sl)
        tp_abs = abs(view.tp - view.entry)
        plan = RiskPlan(
            valid=True, entry=round(view.entry, 6), sl=round(view.sl, 6),
            tp=round(view.tp, 6), rr=view.rr,
            risk_pips=round(risk_abs / pip_size, 1),
            tp_pips=round(tp_abs / pip_size, 1),
            lots=round((self.risk_agent.account_size * self.risk_agent.risk_pct / 100)
                       / (risk_abs / pip_size * pip_value), 2),
            reasons=["entree : retest de zone/cassure M15",
                     "stop : structure (min 0.5 ATR)", "objectif : 1:3 fixe"],
        )
        report.risk_valid = True

        spam = self._spam_blockers("EURUSD", now)
        if spam:
            report.blockers.extend(spam)
            return report

        fund_view = FundamentalView(fund_view.bias, fund_view.score, supports,
                                    fund_view.drivers, fund_view.high_impact_soon)
        signal = Signal(
            pair="EURUSD", direction=view.direction, score=view.score,
            grade=grade_of(view.score, self.grades) or "B",
            session=session_label(now), risk=plan,
            confluences=view.confluences, breakdown=view.breakdown,
            timeframes=view.gates,
            fundamental={"bias": fund_view.bias, "score": fund_view.score,
                         "drivers": fund_view.drivers[:3]},
            risk_label=view.risk_label, dxy_txt=view.dxy_txt,
            news_txt=view.news_txt, macro_bias=view.macro_bias,
            created_at=now.strftime("%Y-%m-%d %H:%M:%S") + " UTC",
        )
        report.signal = signal
        self._dispatch(signal, report)
        return report

    def _legacy_run_pair(self, pair: str, now: pd.Timestamp | None = None) -> CycleReport:
        mtd = self.fetcher.get_multi_timeframe_data(pair, DEFAULT_TIMEFRAMES)
        d1, h4, m15 = mtd.frames.get(_tf("1d")), mtd.frames.get(_tf("4h")), mtd.frames.get(_tf("15m"))
        if d1 is None or h4 is None or m15 is None or d1.empty or h4.empty or m15.empty:
            return CycleReport(
                pair=pair.upper(), generated_at=utc_now_iso(), candidate_direction=None,
                aligned=False, alignment_detail={}, score=0, breakdown={},
                passed_threshold=False, risk_valid=False,
                blockers=["données indisponibles pour D1/H4/M15"],
            )
        return self.run_on_frames(pair, d1, h4, m15, now=now)

    # ------------------------------------------------------------------ #
    def _spam_blockers(self, pair: str, now: pd.Timestamp) -> list[str]:
        blockers: list[str] = []
        if self.db.count_today(pair, now=now.to_pydatetime()) >= self.max_per_day:
            blockers.append(f"quota quotidien atteint ({self.max_per_day}/jour)")
        last = self.db.last_signal_time(pair)
        if last is not None and (now.to_pydatetime() - last) < self.cooldown:
            minutes = int(self.cooldown.total_seconds() // 60 - (now.to_pydatetime() - last).total_seconds() // 60)
            blockers.append(f"cooldown actif (~{minutes} min restantes)")
        return blockers

    def _dispatch(self, signal: Signal, report: CycleReport) -> None:
        try:
            signal.db_id = self.db.insert_signal(signal)
            report.stored = True
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] SQLite : %s", signal.pair, exc)
        try:
            report.telegram_sent = self.telegram.send_signal(signal)
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Telegram : %s", signal.pair, exc)
        try:
            report.card_path = str(save_signal_card(signal, Path("data/cards")))
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] carte SVG : %s", signal.pair, exc)


# --------------------------------------------------------------------------- #
def session_label(now: pd.Timestamp) -> str:
    from .scoring import get_session

    return get_session(now)[0]


def _tf(value: str):
    from ..data.provider import Timeframe

    return Timeframe(value) if value in ("1d", "4h", "15m") else _tf_alias(value)


def _tf_alias(value: str):
    from ..data.provider import timeframe_from_str

    return timeframe_from_str(value)


def _session_for_display(now: pd.Timestamp) -> tuple[str, bool]:
    from .scoring import get_session

    return get_session(now)


def _sig_params(config: Config | None) -> dict:
    """Paramètres de signaux avec valeurs par défaut (config optionnelle)."""
    defaults = {
        "min_score": 70.0,
        "max_per_pair_per_day": 3,
        "grades": [(85, "A+"), (75, "A"), (70, "B")],
        "trigger_max_age_candles": 6,
        "zone_proximity_atr": 1.5,
        "min_rr": 1.5,
        "default_rr": 2.0,
        "risk_pct": 1.0,
        "account_size": 10_000.0,
        "cooldown_minutes": 240,
    }
    if config is None:
        return defaults
    sig = config.signals
    out = dict(defaults)
    out["min_score"] = float(sig.min_score)
    out["max_per_pair_per_day"] = int(sig.max_per_pair_per_day)
    out["grades"] = [(g.min, g.label) for g in sig.grades] or defaults["grades"]
    for key, cast in (("trigger_max_age_candles", int), ("zone_proximity_atr", float),
                      ("min_rr", float), ("default_rr", float), ("risk_pct", float),
                      ("account_size", float), ("cooldown_minutes", int)):
        value = getattr(sig, key, None)
        if value is not None:
            out[key] = cast(value)
    return out
