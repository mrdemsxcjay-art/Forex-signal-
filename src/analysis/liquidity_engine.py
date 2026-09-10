"""Liquidity Engine — PHASE 4 (module SHADOW : non branché au moteur live).

Responsabilités (spec §11-§12) :
    1. CARTOGRAPHIE des niveaux de liquidité :
         - PDH / PDL  (Previous Day High / Low)
         - PWH / PWL  (Previous Week High / Low)
         - EQH / EQL  (equal highs/lows, tolérance en ATR)
         - anciens swing highs / swing lows (structure H4)
       Chaque niveau est étiqueté : côté (au-dessus/en dessous du prix),
       portée (externe = jour/semaine/grands swings, interne = EQH/EQL),
       statut (untouched / swept / broken).
    2. DISTINCTION SWEEP vs TRUE BREAKOUT (§12) — jamais de confusion :
         - SWEEP          : mèche AU-DELÀ du niveau + clôture REVENUE +
                            rejet (la liquidité est prise, le mouvement échoue)
         - TRUE BREAKOUT  : clôture au-delà + DÉPLACEMENT (corps >= x ATR) +
                            ACCEPTATION (n clôtures suivantes tiennent au-delà)
         - FAILED BREAKOUT: clôture au-delà puis réintégration -> trap,
                            classé swept (la cassure a échoué)
    3. RÉPONSE À LA QUESTION §11 : le prix vient-il de PRENDRE une liquidité
       (sweep récent, implication inverse) ou est-il EN ROUTE vers la
       prochaine (niveau intact le plus proche, en ATR) ?

Fonction pure des bougies clôturées (H4 opérationnel + D1 pour PD/PW) :
causal, rejouable, testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .candles import compute_atr, find_swing_points

UPPER_KINDS = ("PDH", "PWH", "EQH", "SWING_HIGH")
LOWER_KINDS = ("PDL", "PWL", "EQL", "SWING_LOW")

STATUS_UNTOUCHED = "untouched"
STATUS_SWEPT = "swept"
STATUS_BROKEN = "broken"

EXTERNAL_KINDS = ("PDH", "PDL", "PWH", "PWL", "SWING_HIGH", "SWING_LOW")
INTERNAL_KINDS = ("EQH", "EQL")


@dataclass(frozen=True)
class LiquidityLevel:
    price: float
    kind: str              # PDH / PDL / PWH / PWL / EQH / EQL / SWING_HIGH / SWING_LOW
    scope: str             # "external" | "internal"
    status: str            # untouched / swept / broken
    time: pd.Timestamp | None = None
    detail: str = ""

    @property
    def is_upper(self) -> bool:
        return self.kind in UPPER_KINDS


@dataclass(frozen=True)
class SweepEvent:
    level_price: float
    kind: str
    direction: str          # "swept_highs" | "swept_lows"
    time: pd.Timestamp
    wick_atr: float
    implication: str        # "bearish" (highs pris) | "bullish" (lows pris)
    detail: str = ""


@dataclass(frozen=True)
class BreakoutEvent:
    level_price: float
    kind: str
    direction: str          # "up" | "down"
    time: pd.Timestamp
    displacement_atr: float
    accepted: bool
    detail: str = ""


@dataclass
class LiquidityInfo:
    levels: list[LiquidityLevel] = field(default_factory=list)
    #: niveaux clés PDH/PDL/PWH/PWL, jamais écrêtés (pour la Daily Map)
    key_levels: dict[str, LiquidityLevel] = field(default_factory=dict)
    nearest_above: LiquidityLevel | None = None    # niveau INTACT le plus proche au-dessus
    nearest_below: LiquidityLevel | None = None
    recent_sweeps: list[SweepEvent] = field(default_factory=list)
    recent_breakouts: list[BreakoutEvent] = field(default_factory=list)
    narrative: str = ""                             # la réponse §11, en une phrase

    @property
    def just_swept_lows(self) -> bool:
        return any(s.direction == "swept_lows" for s in self.recent_sweeps)

    @property
    def just_swept_highs(self) -> bool:
        return any(s.direction == "swept_highs" for s in self.recent_sweeps)


class MarketLiquidityEngine:
    """Cartographie + classification sweep/breakout sur bougies H4 clôturées."""

    def __init__(
        self,
        swing_k: int = 4,
        eq_tol_atr: float = 0.25,        # tolérance de regroupement EQH/EQL
        displacement_atr: float = 0.8,   # corps minimal d'un vrai breakout
        accept_closes: int = 2,          # clôtures d'acceptation consécutives
        scan_window: int = 30,           # bougies H4 analysées pour les interactions
        recent_window: int = 12,         # bougies récentes pour "sweep récent"
        eq_exclude_recent: int = 12,     # bougies exclues du clustering EQ (zone vive)
        max_levels: int = 16,
    ) -> None:
        self.swing_k = int(swing_k)
        self.eq_tol_atr = float(eq_tol_atr)
        self.displacement_atr = float(displacement_atr)
        self.accept_closes = int(accept_closes)
        self.scan_window = int(scan_window)
        self.recent_window = int(recent_window)
        self.eq_exclude_recent = int(eq_exclude_recent)
        self.max_levels = int(max_levels)

    # ------------------------------------------------------------------ #
    def analyze(self, df: pd.DataFrame, df_d1: pd.DataFrame | None = None) -> LiquidityInfo:
        """`df` : H4 clôturées ; `df_d1` : D1 clôturées (PDH/PDL/PWH/PWL)."""
        info = LiquidityInfo()
        if df is None or len(df) < 40:
            info.narrative = "données insuffisantes pour cartographier la liquidité"
            return info

        price = float(df["close"].iloc[-1])
        atr = compute_atr(df, 14)
        levels: list[LiquidityLevel] = []

        # --- 1) Niveaux jour / semaine précédents (D1) --------------------
        if df_d1 is not None and not df_d1.empty:
            levels.extend(self._daily_weekly_levels(df, df_d1))

        # --- 2) EQH / EQL (tolérance ATR, hors zone vive) -------------------
        levels.extend(self._equal_levels(df, atr))

        # --- 3) Anciens swings H4 (hors zone vive) ---------------------------
        levels.extend(self._swing_levels(df, atr))

        if not levels:
            info.narrative = "aucun niveau de liquidité identifiable"
            return info

        # --- 4) Déduplication (niveaux fusionnés à < 0.1 ATR) ----------------
        levels = self._dedup(levels, atr[-1] or 1e-9)

        # --- 5) Classification sweep / breakout par niveau --------------------
        sweeps: list[SweepEvent] = []
        breakouts: list[BreakoutEvent] = []
        classified: list[LiquidityLevel] = []
        for lv in levels:
            status, events = self._classify(df, lv, atr)
            classified.append(LiquidityLevel(lv.price, lv.kind, lv.scope, status,
                                             lv.time, lv.detail))
            sweeps.extend(e for e in events if isinstance(e, SweepEvent))
            breakouts.extend(e for e in events if isinstance(e, BreakoutEvent))

        # --- 6) Niveaux clés (jamais écrêtés) + sélection par proximité ------
        for kind in ("PDH", "PDL", "PWH", "PWL"):
            for lv in classified:
                if lv.kind == kind:
                    info.key_levels[kind] = lv
                    break
        classified.sort(key=lambda l: abs(l.price - price))
        classified = classified[: self.max_levels]

        # --- 7) Niveaux intacts les plus proches + événements récents ----------
        above = [l for l in classified
                 if l.is_upper and l.status == STATUS_UNTOUCHED and l.price > price]
        below = [l for l in classified
                 if not l.is_upper and l.status == STATUS_UNTOUCHED and l.price < price]
        info.levels = classified
        info.nearest_above = min(above, key=lambda l: l.price) if above else None
        info.nearest_below = max(below, key=lambda l: l.price) if below else None

        a_now = float(atr[-1]) or 1e-9
        recent_cutoff = len(df) - 1 - self.recent_window
        info.recent_sweeps = sorted(
            [s for s in sweeps if _idx_of(df, s.time) >= recent_cutoff],
            key=lambda s: s.time)
        info.recent_breakouts = sorted(
            [b for b in breakouts if _idx_of(df, b.time) >= recent_cutoff],
            key=lambda b: b.time)
        info.narrative = self._narrative(info, price, a_now)
        return info

    # ------------------------------------------------------------------ #
    #  Construction des niveaux
    # ------------------------------------------------------------------ #
    @staticmethod
    def _daily_weekly_levels(df: pd.DataFrame, df_d1: pd.DataFrame) -> list[LiquidityLevel]:
        """PDH/PDL (dernier jour CLOTURÉ) et PWH/PWL (dernière semaine CLOTURÉE)."""
        out: list[LiquidityLevel] = []
        last_h4_day = df.index[-1].normalize()
        days = df_d1[df_d1.index.normalize() < last_h4_day]
        if not days.empty:
            prev_day = days.iloc[-1]
            out.append(LiquidityLevel(float(prev_day["high"]), "PDH", "external",
                                      STATUS_UNTOUCHED, days.index[-1],
                                      "plus haut de la veille"))
            out.append(LiquidityLevel(float(prev_day["low"]), "PDL", "external",
                                      STATUS_UNTOUCHED, days.index[-1],
                                      "plus bas de la veille"))
        # semaine précédente (ISO) = la DERNIÈRE semaine complète disponible
        # avant la semaine en cours (pas tout l'historique !)
        last_week = int(df.index[-1].isocalendar()[1])
        d1_weeks = df_d1.index.isocalendar().week.to_numpy()
        before = d1_weeks < last_week
        prev_week_days = df_d1[before]
        if not prev_week_days.empty:
            prev_week_num = int(d1_weeks[before].max())
            prev_week_days = df_d1[d1_weeks == prev_week_num]
            current_week_days = df_d1[d1_weeks == last_week]
            if not current_week_days.empty:  # la semaine en cours a commencé
                out.append(LiquidityLevel(float(prev_week_days["high"].max()), "PWH",
                                          "external", STATUS_UNTOUCHED,
                                          prev_week_days.index[-1],
                                          "plus haut de la semaine passée"))
                out.append(LiquidityLevel(float(prev_week_days["low"].min()), "PWL",
                                          "external", STATUS_UNTOUCHED,
                                          prev_week_days.index[-1],
                                          "plus bas de la semaine passée"))
        return out

    def _equal_levels(self, df: pd.DataFrame, atr) -> list[LiquidityLevel]:
        """EQH/EQL : clusters de swings au même niveau (tolérance ATR)."""
        n = len(df)
        swings = find_swing_points(df, k=self.swing_k)
        old = [s for s in swings if s.index <= n - 1 - self.eq_exclude_recent]
        out: list[LiquidityLevel] = []
        for kind, label in (("high", "EQH"), ("low", "EQL")):
            pts = [s for s in old if s.kind == kind]
            cluster: list = []
            for s in pts:
                if cluster:
                    tol = self.eq_tol_atr * float(atr[s.index] or 1e-9)
                    mean = float(np.mean([x.price for x in cluster]))
                    if abs(s.price - mean) <= tol:
                        cluster.append(s)
                        continue
                    out.extend(self._flush_cluster(cluster, label))
                    cluster = []
                cluster.append(s)
            out.extend(self._flush_cluster(cluster, label))
        return out

    @staticmethod
    def _flush_cluster(cluster: list, label: str) -> list[LiquidityLevel]:
        if len(cluster) >= 2:
            price = float(np.mean([x.price for x in cluster]))
            return [LiquidityLevel(round(price, 6), label, "internal", STATUS_UNTOUCHED,
                                   cluster[-1].time,
                                   f"{len(cluster)} touches regroupées")]
        return []

    def _swing_levels(self, df: pd.DataFrame, atr) -> list[LiquidityLevel]:
        """Anciens swings H4 isolés (non regroupés en EQ) — liquiidté évidente."""
        n = len(df)
        swings = find_swing_points(df, k=self.swing_k)
        old = [s for s in swings if s.index <= n - 1 - self.eq_exclude_recent]
        out = []
        for s in old[-8:]:  # les 8 plus récents suffisent (cap natif)
            kind = "SWING_HIGH" if s.kind == "high" else "SWING_LOW"
            out.append(LiquidityLevel(round(float(s.price), 6), kind, "external",
                                      STATUS_UNTOUCHED, s.time, "swing structurel H4"))
        return out

    # ------------------------------------------------------------------ #
    #  Classification SWEEP vs TRUE BREAKOUT (§12)
    # ------------------------------------------------------------------ #
    def _classify(self, df: pd.DataFrame, lv: LiquidityLevel, atr):
        """Première interaction du prix avec le niveau -> statut + événements.

        Upper level (PDH, EQH, SWING_HIGH...) :
            close > niveau :
                corps >= displacement_atr ET accept_closes clôtures suivantes
                tiennent au-delà  -> TRUE BREAKOUT (broken)
                sinon si une clôture suivante réintègre      -> FAILED BREAKOUT
                                                            -> swept (trap)
            mèche > niveau mais close <= niveau              -> SWEEP
        Miroir pour les lower levels.
        """
        n = len(df)
        i_start = self._scan_start(df, lv)
        closes = df["close"].to_numpy()
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        events: list[SweepEvent | BreakoutEvent] = []

        for i in range(max(i_start, 0), n):
            a = float(atr[i]) or 1e-9
            if lv.is_upper:
                if closes[i] > lv.price:                      # clôture au-delà
                    body = abs(closes[i] - opens[i])
                    disp = body / a
                    accepted = all(closes[j] > lv.price
                                   for j in range(i + 1, min(i + 1 + self.accept_closes, n)))
                    reintegrated = any(closes[j] <= lv.price
                                       for j in range(i + 1, min(i + 1 + self.accept_closes + 2, n)))
                    if disp >= self.displacement_atr and accepted:
                        events.append(BreakoutEvent(lv.price, lv.kind, "up",
                                                    df.index[i], round(disp, 2), True,
                                                    f"cassure acceptée ({self.accept_closes} clôtures)"))
                        return STATUS_BROKEN, events
                    if reintegrated:
                        events.append(SweepEvent(lv.price, lv.kind, "swept_highs",
                                                 df.index[i], round((highs[i] - lv.price) / a, 2),
                                                 "bearish",
                                                 "cassure non acceptée puis réintégration (trap)"))
                        return STATUS_SWEPT, events
                    # clôture au-delà, verdict en attente -> considéré cassé
                    events.append(BreakoutEvent(lv.price, lv.kind, "up", df.index[i],
                                                round(disp, 2), False, "en attente d'acceptation"))
                    return STATUS_BROKEN, events
                if highs[i] > lv.price:                        # mèche seulement
                    wick = (highs[i] - lv.price) / a
                    events.append(SweepEvent(lv.price, lv.kind, "swept_highs",
                                             df.index[i], round(wick, 2), "bearish",
                                             "mèche au-dessus, clôture revenue"))
                    return STATUS_SWEPT, events
            else:
                if closes[i] < lv.price:
                    body = abs(closes[i] - opens[i])
                    disp = body / a
                    accepted = all(closes[j] < lv.price
                                   for j in range(i + 1, min(i + 1 + self.accept_closes, n)))
                    reintegrated = any(closes[j] >= lv.price
                                       for j in range(i + 1, min(i + 1 + self.accept_closes + 2, n)))
                    if disp >= self.displacement_atr and accepted:
                        events.append(BreakoutEvent(lv.price, lv.kind, "down",
                                                    df.index[i], round(disp, 2), True,
                                                    f"cassure acceptée ({self.accept_closes} clôtures)"))
                        return STATUS_BROKEN, events
                    if reintegrated:
                        events.append(SweepEvent(lv.price, lv.kind, "swept_lows",
                                                 df.index[i], round((lv.price - lows[i]) / a, 2),
                                                 "bullish",
                                                 "cassure non acceptée puis réintégration (trap)"))
                        return STATUS_SWEPT, events
                    events.append(BreakoutEvent(lv.price, lv.kind, "down", df.index[i],
                                                round(disp, 2), False, "en attente d'acceptation"))
                    return STATUS_BROKEN, events
                if lows[i] < lv.price:
                    wick = (lv.price - lows[i]) / a
                    events.append(SweepEvent(lv.price, lv.kind, "swept_lows",
                                             df.index[i], round(wick, 2), "bullish",
                                             "mèche en dessous, clôture revenue"))
                    return STATUS_SWEPT, events
        return STATUS_UNTOUCHED, events

    def _scan_start(self, df: pd.DataFrame, lv: LiquidityLevel) -> int:
        """Début de scan : après la création du niveau, dans la fenêtre."""
        n = len(df)
        default = n - self.scan_window
        if lv.time is None:
            return default
        ts = pd.Timestamp(lv.time)
        if ts.tzinfo is None:
            ts = ts.tz_localize(df.index.tz)
        pos = df.index.searchsorted(ts)
        return max(default, pos)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedup(levels: list[LiquidityLevel], atr_now: float) -> list[LiquidityLevel]:
        """Fusionne les niveaux quasi identiques (< 0.1 ATR), priorité externe."""
        tol = 0.1 * atr_now
        out: list[LiquidityLevel] = []
        for lv in sorted(levels, key=lambda l: (l.scope != "external", l.price)):
            if out and abs(out[-1].price - lv.price) <= tol and out[-1].is_upper == lv.is_upper:
                continue
            out.append(lv)
        return out

    @staticmethod
    def _narrative(info: LiquidityInfo, price: float, atr_now: float) -> str:
        parts: list[str] = []
        if info.recent_sweeps:
            s = info.recent_sweeps[-1]
            parts.append(
                f"le prix vient de PRENDRE la liquidité ({s.kind} @ {s.level_price:.5f}, "
                f"implication {s.implication})")
        if info.nearest_above:
            d = (info.nearest_above.price - price) / atr_now
            parts.append(f"prochaine liquidité AU-DESSUS : {info.nearest_above.kind} "
                         f"{info.nearest_above.price:.5f} ({d:.1f} ATR)")
        if info.nearest_below:
            d = (price - info.nearest_below.price) / atr_now
            parts.append(f"prochaine liquidité EN DESSOUS : {info.nearest_below.kind} "
                         f"{info.nearest_below.price:.5f} ({d:.1f} ATR)")
        return " ; ".join(parts) if parts else "aucune liquidité pertinente identifiée"


def _idx_of(df: pd.DataFrame, ts) -> int:
    try:
        return df.index.get_indexer([pd.Timestamp(ts)])[0]
    except Exception:  # noqa: BLE001
        return -1
