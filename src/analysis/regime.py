"""Market Regime Engine — PHASE 2 (module SHADOW : non branché au moteur live).

Détermine le régime de marché AVANT toute recherche de signal (spec §3) :

    INSUFFICIENT_DATA  pas assez de bougies pour conclure        -> NO_TRADE
    CHAOTIC            alternances structurelles excessives      -> NO_TRADE
    BREAKOUT           cassure acceptée d'un range préexistant
    BREAKOUT_RETEST    cassure puis réintégration (faux break ?)
    TRANSITION         CHoCH récent / aucune structure dominante -> prudence
    TREND_UP           majorité de HH+HL (structure haussière)
    TREND_DOWN         majorité de LH+LL
    RANGE              regroupement des sommets ET des creux     -> MID = NO_TRADE

Principes d'implémentation :
    - FONCTION PURE des bougies H4 CLÔTURÉES (aucune fuite du futur :
      seuls les swings confirmés et les événements structurels déjà
      observables à l'instant évalué sont utilisés) ;
    - hiérarchie de décision fixe (CHAOTIC > BREAKOUT > TRANSITION-récent >
      TREND > RANGE > TRANSITION par défaut) — une règle en amont ne peut
      pas être annulée par une règle en aval ;
    - chaque paramètre expose des candidats calibrés (spec §30) — voir
      scripts/test_regime.py (grille) et docs/PHASE2_REPORT.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .candles import compute_atr, find_swing_points
from .structure import detect_structure

REGIME_INSUFFICIENT = "INSUFFICIENT_DATA"
REGIME_CHAOTIC = "CHAOTIC"
REGIME_BREAKOUT = "BREAKOUT"
REGIME_BREAKOUT_RETEST = "BREAKOUT_RETEST"
REGIME_TRANSITION = "TRANSITION"
REGIME_TREND_UP = "TREND_UP"
REGIME_TREND_DOWN = "TREND_DOWN"
REGIME_RANGE = "RANGE"

#: Régimes pour lesquels la future Location/Setup gate doit refuser (prudence).
NO_TRADE_REGIMES = (REGIME_INSUFFICIENT, REGIME_CHAOTIC, REGIME_TRANSITION,
                    REGIME_BREAKOUT_RETEST)


@dataclass
class RegimeInfo:
    """Verdict du régime + le contexte nécessaire aux phases suivantes."""

    regime: str
    confidence: float                 # 0-1
    detail: str                       # explication lisible (une ligne)
    range_high: float | None = None   # bornes du range détecté (si pertinent)
    range_low: float | None = None
    range_mid: float | None = None
    position_pct: float | None = None  # position du prix dans le range (0-100)
    premium_discount: str | None = None
    breakout_direction: str | None = None

    @property
    def is_no_trade_regime(self) -> bool:
        return self.regime in NO_TRADE_REGIMES


class MarketRegimeEngine:
    """Détection du régime sur bougies H4 clôturées (fenêtre glissante)."""

    def __init__(
        self,
        window: int = 120,              # bougies H4 analysées (~20 jours)
        min_candles: int = 60,
        swing_k: int = 4,  # calibré : k=2 trop bruité sur H4 réel (90% TRANSITION)
        trend_majority: float = 0.6,  # calibré sur 90j réels : 0.7 ne détectait aucune tendance    # fraction HH+HL (ou LH+LL) requise
        min_trend_comparisons: int = 4,
        range_touch_tol_atr: float = 0.5,   # tolérance de regroupement des extrêmes
        range_min_width_atr: float = 3.0,   # largeur minimale d'un range traditionnel
        range_boundary_drift_atr: float = 0.5,  # dérive max des bornes (ATR / 10 bougies)
        breakout_disp_atr: float = 1.0,     # déplacement minimal au-delà de la borne
        breakout_accept_closes: int = 2,    # clôtures consécutives hors range
        recent_zone: int = 12,          # bougies récentes exclues du clustering range
        choch_recency: int = 12,        # bougies : un CHoCH récent => TRANSITION
        whipsaw_alternations: int = 4,  # alternances de cassure => CHAOTIC
        whipsaw_window: int = 48,
    ) -> None:
        self.window = int(window)
        self.min_candles = int(min_candles)
        self.swing_k = int(swing_k)
        self.trend_majority = float(trend_majority)
        self.min_trend_comparisons = int(min_trend_comparisons)
        self.range_touch_tol_atr = float(range_touch_tol_atr)
        self.range_min_width_atr = float(range_min_width_atr)
        self.range_boundary_drift_atr = float(range_boundary_drift_atr)
        self.breakout_disp_atr = float(breakout_disp_atr)
        self.breakout_accept_closes = int(breakout_accept_closes)
        self.recent_zone = int(recent_zone)
        self.choch_recency = int(choch_recency)
        self.whipsaw_alternations = int(whipsaw_alternations)
        self.whipsaw_window = int(whipsaw_window)

    # ------------------------------------------------------------------ #
    def detect(self, df: pd.DataFrame) -> RegimeInfo:
        """Classifie le régime au dernier close de `df` (H4 clôturées)."""
        if df is None or len(df) < self.min_candles:
            return RegimeInfo(REGIME_INSUFFICIENT, 0.0,
                              f"{0 if df is None else len(df)} bougies < {self.min_candles} requises")

        win = df.tail(self.window)
        n = len(win)
        price = float(win["close"].iloc[-1])
        atr = float(compute_atr(win, 14)[-1]) or 1e-9
        # ATR CONTEXTUEL : mesuré sur la période ANTÉRIEURE à la zone récente
        # (un breakout gonfle l'ATR et ferait rejeter le range qu'il casse)
        ctx_end = max(30, n - self.recent_zone)
        atr_ctx = float(compute_atr(win.iloc[:ctx_end], 14)[-1]) or atr
        swings = self._dedup_plateaus(find_swing_points(win, k=self.swing_k))
        events, _ = detect_structure(win, swings, prefix="RG")

        # --- 1) CHAOTIC : alternances structurelles excessives -------------
        recent_events = [e for e in events if e["break_index"] >= n - self.whipsaw_window]
        alternations = sum(
            1 for a, b in zip(recent_events, recent_events[1:])
            if a["direction"] != b["direction"]
        )
        if alternations >= self.whipsaw_alternations:
            return RegimeInfo(
                REGIME_CHAOTIC, min(1.0, alternations / (self.whipsaw_alternations + 2)),
                f"{alternations} alternances de cassure sur {self.whipsaw_window} bougies",
            )

        # --- Range préexistant (clustering SANS la zone récente) -----------
        old = [s for s in swings if s.index <= n - 1 - self.recent_zone]
        highs = [s.price for s in old if s.kind == "high"]
        lows = [s.price for s in old if s.kind == "low"]
        range_info = self._range_stats(highs, lows, atr_ctx)
        rh, rl, touches_h, touches_l = range_info
        prior_range_valid = (
            rh is not None
            and touches_h >= 2 and touches_l >= 2
            and (rh - rl) >= self.range_min_width_atr * atr_ctx
            and self._boundaries_flat(highs, lows, atr_ctx)
        )

        # --- 2) BREAKOUT / BREAKOUT_RETEST ---------------------------------
        if prior_range_valid:
            close = win["close"]
            recent_closes = close.iloc[max(0, n - 10):]
            disp = self.breakout_disp_atr * atr
            up_ok = price > rh and (price - rh) >= disp * 0  # close au-dessus
            dn_ok = price < rl
            if up_ok or dn_ok:
                direction = "up" if up_ok else "down"
                accepted = all(
                    c > rh if up_ok else c < rl
                    for c in close.iloc[-self.breakout_accept_closes:]
                )
                conf = 0.9 if accepted else 0.55
                label = REGIME_BREAKOUT
                detail = (f"cassure {'haussière' if up_ok else 'baissière'} du range "
                          f"[{rl:.5f}, {rh:.5f}] "
                          + ("acceptée (clôtures consécutives hors range)" if accepted
                             else "non confirmée (clôture unique au-delà)"))
                return RegimeInfo(label, conf, detail, rh, rl, (rh + rl) / 2,
                                  self._pos(price, rh, rl), None, direction)
            beyond_recently = (
                (recent_closes > rh).any() or (recent_closes < rl).any()
            )
            if beyond_recently:
                return RegimeInfo(
                    REGIME_BREAKOUT_RETEST, 0.6,
                    f"cassure récente puis réintégration du range "
                    f"[{rl:.5f}, {rh:.5f}] — faux breakout potentiel",
                    rh, rl, (rh + rl) / 2, self._pos(price, rh, rl),
                )

        # --- 3) TRANSITION : CHoCH récent ----------------------------------
        recent_choch = [e for e in events
                        if e["type"] == "CHoCH"
                        and e["break_index"] >= n - 1 - self.choch_recency]
        if recent_choch:
            e = recent_choch[-1]
            return RegimeInfo(
                REGIME_TRANSITION, 0.65,
                f"CHoCH {e['direction']} il y a {n - 1 - e['break_index']} bougies — "
                "retournement en formation, prudence",
            )

        # --- 4) TREND_UP / TREND_DOWN : majorité structurelle ---------------
        up_frac, down_frac, comparisons = self._trend_fractions(swings)
        if comparisons >= self.min_trend_comparisons:
            if up_frac >= self.trend_majority:
                return RegimeInfo(REGIME_TREND_UP, up_frac,
                                  f"{comparisons} comparaisons de swings, "
                                  f"{up_frac * 100:.0f}% de HH/HL")
            if down_frac >= self.trend_majority:
                return RegimeInfo(REGIME_TREND_DOWN, down_frac,
                                  f"{comparisons} comparaisons de swings, "
                                  f"{down_frac * 100:.0f}% de LH/LL")

        # --- 5) RANGE --------------------------------------------------------
        if prior_range_valid and rl <= price <= rh:
            pos = self._pos(price, rh, rl)
            pd_label = ("discount" if pos < 45 else
                        "premium" if pos > 55 else "equilibrium")
            return RegimeInfo(
                REGIME_RANGE,
                min(1.0, (touches_h + touches_l) / 8),
                f"range [{rl:.5f}, {rh:.5f}] — {touches_h} tests du haut, "
                f"{touches_l} du bas ; prix à {pos:.0f}% du range ({pd_label})",
                rh, rl, (rh + rl) / 2, pos, pd_label,
            )

        # --- 6) Défaut : structure indéterminée ------------------------------
        width = (rh - rl) if rh is not None else None
        comp_txt = (f"compression ({width / atr:.1f} ATR)" if width is not None
                    and width < self.range_min_width_atr * atr
                    else "aucune majorité structurelle")
        return RegimeInfo(REGIME_TRANSITION, 0.4, comp_txt)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedup_plateaus(swings):
        """Fusionne les swings adjacents de même type au MÊME niveau.

        Phénomène réel : au sommet, la bougie suivante ouvre à la clôture
        précédente -> deux highs identiques consécutifs -> la fractale
        compte le même sommet deux fois et fausse HH/HL. On ne garde que
        la première occurrence d'un plateau (indices adjacents, prix égal).
        """
        out = []
        for s in swings:
            if out:
                last = out[-1]
                if (s.kind == last.kind
                        and s.index - last.index <= 3
                        and abs(s.price - last.price) < 1e-9):
                    continue
            out.append(s)
        return out

    def _range_stats(self, highs: list[float], lows: list[float], atr: float):
        """Borne haute/basse du regroupement + nombre de touches."""
        if not highs or not lows:
            return None, None, 0, 0
        rh, rl = max(highs), min(lows)
        tol = self.range_touch_tol_atr * atr  # regroupement des extrêmes
        th = sum(1 for h in highs if rh - h <= max(tol, 1e-9))
        tl = sum(1 for l in lows if l - rl <= max(tol, 1e-9))
        return rh, rl, th, tl

    def _boundaries_flat(self, highs: list[float], lows: list[float], atr: float) -> bool:
        """Un VRAI range a des bornes horizontales : la dérive des sommets et
        des creux (régression) doit rester faible, sinon c'est un canal de
        tendance (les 2 touches aux extrémités d'un trend ne sont pas un range).
        """
        if len(highs) < 2 or len(lows) < 2:
            return False
        max_drift = self.range_boundary_drift_atr * atr / 10.0  # par bougie
        for prices in (highs, lows):
            x = np.arange(len(prices), dtype=float)
            slope = float(np.polyfit(x, np.asarray(prices), 1)[0])
            if abs(slope) > max_drift:
                return False
        return True

    @staticmethod
    def _trend_fractions(swings) -> tuple[float, float, int]:
        """Fractions HH/HL (up) et LH/LL (down) sur swings chronologiques."""
        highs = [s.price for s in swings if s.kind == "high"]
        lows = [s.price for s in swings if s.kind == "low"]
        up = down = total = 0
        for a, b in zip(highs, highs[1:]):
            total += 1
            up += b > a
            down += b < a
        for a, b in zip(lows, lows[1:]):
            total += 1
            up += b > a
            down += b < a
        if total == 0:
            return 0.0, 0.0, 0
        return up / total, down / total, total

    @staticmethod
    def _pos(price: float, rh: float, rl: float) -> float:
        span = (rh - rl) or 1e-9
        return round((price - rl) / span * 100, 1)
