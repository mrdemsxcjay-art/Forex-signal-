"""Price Action Engine — PHASE 5 (module SHADOW : non branché au moteur live).

Responsabilité (spec §14) : analyser les COMPORTEMENTS du prix, pas des
indicateurs, et les quantifier (force 0-1) pour la future Location/Scenario
Engine (phase 7).

    Bougies uniques / paires :
        pin_bar, rejection (d'un niveau), engulfing, inside_bar,
        displacement (corps >= x ATR), absorption (bougie contraire avalée),
        failed_breakout (clôture au-delà d'un niveau puis réintégration)

    Comportements de fenêtre :
        compression (ATR5 << ATR50), expansion (ATR5 >> ATR50),
        impulse (bougies directionnelles cumulées), consolidation
        (bande étroite), character_change (cassure contre-tendance AVEC
        déplacement — le "changement de caractère" au niveau price action)

RÈGLE ABSOLUE (§14) : AUCUN pattern ne déclenche un signal seul.
`confirmation(direction)` ne retourne qu'une INFORMATION — c'est le
consommateur (gates hiérarchiques) qui décide, jamais ce module.

Fonction pure des bougies clôturées : causal, rejouable, testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .candles import compute_atr, find_swing_points
from .indicators import bear_pin_bar, bearish_engulfing, bull_pin_bar, bullish_engulfing

#: Patterns directionnels utilisables comme confirmation (le reste = contexte).
CONFIRMATION_PATTERNS = ("pin_bar", "engulfing", "rejection", "displacement",
                         "absorption", "failed_breakout", "character_change")


@dataclass(frozen=True)
class Pattern:
    name: str
    direction: str            # bullish / bearish / neutral
    index: int
    time: pd.Timestamp
    strength: float           # 0-1 (qualité du pattern)
    detail: str = ""


@dataclass
class PriceActionInfo:
    patterns: list[Pattern] = field(default_factory=list)
    behaviors: dict[str, bool] = field(default_factory=dict)
    behavior_detail: dict[str, str] = field(default_factory=dict)
    impulse_direction: str | None = None
    character_change: Pattern | None = None

    def confirmation(self, direction: str) -> Pattern | None:
        """Meilleure confirmation récente pour une direction — INFORMATION,
        pas un déclencheur (règle §14)."""
        candidates = [p for p in self.patterns
                      if p.name in CONFIRMATION_PATTERNS and p.direction == direction]
        if self.character_change and self.character_change.direction == direction:
            candidates.append(self.character_change)
        return max(candidates, key=lambda p: p.strength) if candidates else None

    @property
    def compressed(self) -> bool:
        return bool(self.behaviors.get("compression"))

    @property
    def expanding(self) -> bool:
        return bool(self.behaviors.get("expansion"))


class PriceActionEngine:
    """Détection et quantification des comportements de prix."""

    def __init__(
        self,
        swing_k: int = 4,
        lookback: int = 8,               # bougies récentes scannées (2 h en M15 :
                                         #  un sweep -> rejet -> confirmation peut
                                         #  prendre 1-3 h ; 3 bougies = invisible)
        wick_ratio: float = 2.0,         # pin bar : mèche / corps
        displacement_atr: float = 0.8,   # corps minimal d'un déplacement
        compression_ratio: float = 0.6,  # ATR5 / ATR50 <= x -> compression
        expansion_ratio: float = 1.6,    # ATR5 / ATR50 >= x -> expansion
        impulse_count: int = 3,          # bougies directionnelles consécutives
        impulse_total_atr: float = 2.0,  # amplitude cumulée minimale
        consolidation_band_atr: float = 2.0,  # largeur max de la bande (x ATR50)
        consolidation_window: int = 10,
        rejection_buffer_atr: float = 0.05,  # clôture revenue au-delà du niveau
        min_pattern_body_atr: float = 0.3,   # corps minimal d'un pattern significatif
    ) -> None:
        self.swing_k = int(swing_k)
        self.lookback = int(lookback)
        self.wick_ratio = float(wick_ratio)
        self.displacement_atr = float(displacement_atr)
        self.compression_ratio = float(compression_ratio)
        self.expansion_ratio = float(expansion_ratio)
        self.impulse_count = int(impulse_count)
        self.impulse_total_atr = float(impulse_total_atr)
        self.consolidation_band_atr = float(consolidation_band_atr)
        self.consolidation_window = int(consolidation_window)
        self.rejection_buffer_atr = float(rejection_buffer_atr)
        self.min_pattern_body_atr = float(min_pattern_body_atr)

    # ------------------------------------------------------------------ #
    def analyze(self, df: pd.DataFrame,
                levels: list[float] | None = None) -> PriceActionInfo:
        """`df` : bougies clôturées d'un timeframe ; `levels` : niveaux de
        liquidité (Phase 4) pour rejection / failed_breakout / retest."""
        info = PriceActionInfo()
        if df is None or len(df) < 60:
            info.behavior_detail["data"] = "données insuffisantes (< 60 bougies)"
            return info

        n = len(df)
        atr = compute_atr(df, 14)
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()

        patterns: list[Pattern] = []

        # --- patterns sur les bougies récentes ----------------------------
        for i in range(max(1, n - self.lookback), n):
            a = float(atr[i]) or 1e-9
            body = abs(closes[i] - opens[i]) or 1e-12

            meaningful = body >= self.min_pattern_body_atr * a  # anti-bruit
            if meaningful and bull_pin_bar(df, i, self.wick_ratio):
                lower = min(opens[i], closes[i]) - lows[i]
                patterns.append(Pattern(
                    "pin_bar", "bullish", i, df.index[i],
                    min(1.0, lower / (2.5 * body)),
                    f"mèche basse {lower / body:.1f}x le corps"))
            if meaningful and bear_pin_bar(df, i, self.wick_ratio):
                upper = highs[i] - max(opens[i], closes[i])
                patterns.append(Pattern(
                    "pin_bar", "bearish", i, df.index[i],
                    min(1.0, upper / (2.5 * body)),
                    f"mèche haute {upper / body:.1f}x le corps"))

            if meaningful and bullish_engulfing(df, i):
                prev_body = abs(closes[i - 1] - opens[i - 1]) or 1e-12
                patterns.append(Pattern(
                    "engulfing", "bullish", i, df.index[i],
                    min(1.0, body / (2 * prev_body)),
                    "bougie haussière avale la baissière précédente"))
            if meaningful and bearish_engulfing(df, i):
                prev_body = abs(closes[i - 1] - opens[i - 1]) or 1e-12
                patterns.append(Pattern(
                    "engulfing", "bearish", i, df.index[i],
                    min(1.0, body / (2 * prev_body)),
                    "bougie baissière avale la haussière précédente"))

            if highs[i] < highs[i - 1] and lows[i] > lows[i - 1]:
                patterns.append(Pattern(
                    "inside_bar", "neutral", i, df.index[i], 0.5,
                    "compression locale (inside bar)"))

            if body >= self.displacement_atr * a:
                direction = "bullish" if closes[i] > opens[i] else "bearish"
                patterns.append(Pattern(
                    "displacement", direction, i, df.index[i],
                    min(1.0, body / (2.0 * a)),
                    f"corps {body / a:.1f} ATR"))

            if meaningful:
                patterns.extend(self._absorption(df, i))

        # --- interactions avec les niveaux (rejection / failed breakout) ---
        if levels:
            for level in levels:
                patterns.extend(self._level_patterns(df, float(level), atr))

        # --- comportements de fenêtre --------------------------------------
        atr5 = self._mean_tr(df, 5)
        atr50 = self._mean_tr(df, 50) or 1e-9
        ratio = atr5 / atr50
        info.behaviors["compression"] = ratio <= self.compression_ratio
        info.behaviors["expansion"] = ratio >= self.expansion_ratio
        info.behavior_detail["volatility"] = f"ATR5/ATR50 = {ratio:.2f}"

        w = self.consolidation_window
        band = float(highs[-w:].max() - lows[-w:].min())
        info.behaviors["consolidation"] = band <= self.consolidation_band_atr * atr50
        info.behavior_detail["consolidation"] = f"bande {w} bougies = {band / atr50:.1f} ATR50"

        impulse_dir, impulse_size = self._impulse(df, atr)
        info.behaviors["impulse"] = impulse_dir is not None
        info.impulse_direction = impulse_dir
        if impulse_dir:
            info.behavior_detail["impulse"] = (
                f"{self.impulse_count} bougies {impulse_dir}, "
                f"{impulse_size:.1f} ATR cumulés")

        # --- changement de caractère (cassure + déplacement) ----------------
        info.character_change = self._character_change(df, atr)

        info.patterns = patterns
        return info

    # ------------------------------------------------------------------ #
    def _absorption(self, df: pd.DataFrame, i: int) -> list[Pattern]:
        """Bougie contraire entièrement absorbée (range avalé + clôture au-delà)."""
        o0, h0, l0, c0 = (float(df[k].iloc[i - 1]) for k in ("open", "high", "low", "close"))
        o1, h1, l1, c1 = (float(df[k].iloc[i]) for k in ("open", "high", "low", "close"))
        out: list[Pattern] = []
        if c0 < o0 and c1 > o1 and h1 >= h0 and l1 <= l0 and c1 > h0:
            out.append(Pattern("absorption", "bullish", i, df.index[i], 0.8,
                               "bougie baissière absorbée, clôture au-dessus de son high"))
        if c0 > o0 and c1 < o1 and h1 >= h0 and l1 <= l0 and c1 < l0:
            out.append(Pattern("absorption", "bearish", i, df.index[i], 0.8,
                               "bougie haussière absorbée, clôture sous son low"))
        return out

    def _level_patterns(self, df: pd.DataFrame, level: float, atr) -> list[Pattern]:
        """Rejection et failed breakout autour d'un niveau de liquidité."""
        n = len(df)
        out: list[Pattern] = []
        for i in range(max(1, n - self.lookback - 1), n):
            a = float(atr[i]) or 1e-9
            buf = self.rejection_buffer_atr * a
            o, h, l, c = (float(df[k].iloc[i]) for k in ("open", "high", "low", "close"))
            # niveau SUPÉRIEUR : tentative haussière rejetée -> bearish
            if h > level and c < level - buf:
                out.append(Pattern(
                    "rejection", "bearish", i, df.index[i],
                    min(1.0, (h - level) / a + (level - c) / (2 * a)),
                    f"mèche au-dessus de {level:.5f}, rejet"))
            # niveau INFÉRIEUR : tentative baissière rejetée -> bullish
            if l < level and c > level + buf:
                out.append(Pattern(
                    "rejection", "bullish", i, df.index[i],
                    min(1.0, (level - l) / a + (c - level) / (2 * a)),
                    f"mèche sous {level:.5f}, rejet"))
            # failed breakout : clôture au-delà PUIS clôture revenue
            if i >= 1:
                c_prev = float(df["close"].iloc[i - 1])
                if c_prev > level and c < level:
                    out.append(Pattern(
                        "failed_breakout", "bearish", i, df.index[i],
                        min(1.0, (c_prev - level) / a + 0.5),
                        f"cassure de {level:.5f} non acceptée, réintégration"))
                if c_prev < level and c > level:
                    out.append(Pattern(
                        "failed_breakout", "bullish", i, df.index[i],
                        min(1.0, (level - c_prev) / a + 0.5),
                        f"cassure de {level:.5f} non acceptée, réintégration"))
        return out

    def _impulse(self, df: pd.DataFrame, atr) -> tuple[str | None, float]:
        """Les `impulse_count` DERNIÈRES bougies toutes même direction,
        cumulant >= impulse_total_atr."""
        n = len(df)
        start = n - self.impulse_count
        if start < 1:
            return None, 0.0
        opens = df["open"].to_numpy()
        closes = df["close"].to_numpy()
        bodies = [closes[j] - opens[j] for j in range(start, n)]
        a = float(atr[start]) or 1e-9
        if all(b > 0 for b in bodies) and sum(bodies) / a >= self.impulse_total_atr:
            return "bullish", sum(bodies) / a
        if all(b < 0 for b in bodies) and -sum(bodies) / a >= self.impulse_total_atr:
            return "bearish", -sum(bodies) / a
        return None, 0.0

    def _character_change(self, df: pd.DataFrame, atr) -> Pattern | None:
        """Changement de caractère : clôture AU-DELÀ du dernier swing confirmé
        contraire, AVEC déplacement (c'est un CHoCH au niveau price action)."""
        n = len(df)
        swings = find_swing_points(df, k=self.swing_k)
        # dédoublonnage des plateaux (leçon phase 2)
        dedup: list = []
        for s in swings:
            if dedup and s.kind == dedup[-1].kind and s.index - dedup[-1].index <= 3 \
                    and abs(s.price - dedup[-1].price) < 1e-9:
                continue
            dedup.append(s)
        confirmed = [s for s in dedup if s.confirm_index <= n - 2]
        if not confirmed:
            return None
        price = float(df["close"].iloc[-1])
        body = abs(float(df["close"].iloc[-1]) - float(df["open"].iloc[-1]))
        a = float(atr[-1]) or 1e-9
        last_high = [s for s in confirmed if s.kind == "high"]
        last_low = [s for s in confirmed if s.kind == "low"]
        if last_low and price < last_low[-1].price and body >= self.displacement_atr * a:
            return Pattern("character_change", "bearish", n - 1, df.index[-1],
                           min(1.0, body / (2 * a)),
                           f"clôture sous le swing low {last_low[-1].price:.5f} avec déplacement")
        if last_high and price > last_high[-1].price and body >= self.displacement_atr * a:
            return Pattern("character_change", "bullish", n - 1, df.index[-1],
                           min(1.0, body / (2 * a)),
                           f"clôture au-dessus du swing high {last_high[-1].price:.5f} avec déplacement")
        return None

    @staticmethod
    def _mean_tr(df: pd.DataFrame, n: int) -> float:
        high, low, close = df["high"], df["low"], df["close"]
        prev = close.shift(1)
        tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        return float(tr.tail(n).mean() or 0.0)
