"""Market Structure Engine — PHASE 3 (module SHADOW : non branché au moteur live).

Responsabilités (spec §9-§10) :
    1. LABELLISATION des swings : HH / HL / LH / LL (+ EQH/EQL pour les
       égalités, pont vers le Liquidity Engine de la phase 4) ;
    2. MSS (Market Structure Shift) : un CHoCH AVEC déplacement (displacement)
       — la cassure de structure confirmée par une bougie de conviction,
       à distinguer du simple CHoCH qui peut n'être qu'un frémissement ;
    3. STRUCTURE INTERNE vs EXTERNE : externe = D1/H4 (le contexte qui
       commande), interne = H1/M30/M15/M5 (l'exécution) ;
    4. HIÉRARCHIE MULTI-TIMEFRAME : une confirmation interne ne peut JAMAIS
       inverser un contexte externe (spec §10) — un M15 baissier dans un D1
       haussier est classé PULLBACK_IN_UPTREND (opportunité de continuation),
       jamais comme un retournement.

Fonctions pures sur bougies clôturées : rejouables, testables, sans fuite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .candles import compute_atr, find_swing_points
from .structure import detect_structure

#: Timeframes de structure externe (contexte) vs interne (exécution).
EXTERNAL_TFS = ("D1", "H4")
INTERNAL_TFS = ("H1", "M30", "M15", "M5")

#: Alignements possibles du verdict multi-timeframe.
ALIGNED_UP = "ALIGNED_UP"
ALIGNED_DOWN = "ALIGNED_DOWN"
PULLBACK_UP = "PULLBACK_IN_UPTREND"      # interne contre externe haussier
PULLBACK_DOWN = "PULLBACK_IN_DOWNTREND"  # interne contre externe baissier
EXTERNAL_UNCLEAR = "EXTERNAL_UNCLEAR"
INSUFFICIENT = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class SwingLabel:
    kind: str          # "high" | "low"
    price: float
    index: int
    time: pd.Timestamp
    label: str         # HH | HL | LH | LL | EQH | EQL | "H?" | "L?" (premier)


@dataclass
class StructureSummary:
    """Photographie structurelle d'un timeframe."""

    timeframe: str
    role: str                        # "external" | "internal"
    trend: str | None                # bullish / bearish / None
    labels: list[str] = field(default_factory=list)      # séquence récente
    counts: dict = field(default_factory=dict)           # HH/HL/LH/LL/EQH/EQL
    last_event: dict | None = None    # BOS/CHoCH le plus récent
    last_mss: dict | None = None      # dernier MSS (CHoCH + displacement)
    n_swings: int = 0
    detail: str = ""


@dataclass(frozen=True)
class StructureVerdict:
    """Verdict hiérarchique multi-timeframe (la spec §10 en code)."""

    external_trend: str | None
    internal_trend: str | None
    alignment: str
    allowed_direction: str | None    # "LONG" / "SHORT" / None
    description: str

    @property
    def is_tradeable_context(self) -> bool:
        return self.allowed_direction is not None


class MarketStructureEngine:
    """Labellisation + MSS + hiérarchie interne/externe."""

    def __init__(self, swing_k: int = 4, displacement_atr: float = 0.8) -> None:
        self.swing_k = int(swing_k)
        self.displacement_atr = float(displacement_atr)

    # ------------------------------------------------------------------ #
    #  1. Labellisation des swings
    # ------------------------------------------------------------------ #
    def label_swings(self, df: pd.DataFrame) -> list[SwingLabel]:
        """HH/HL/LH/LL par comparaison chronologique des swings homologues.

        Égalité (à 1e-9) -> EQH/EQL : information de liquidité (phase 4).
        Le premier swing de chaque type reste indéterminé ("H?"/"L?").
        """
        swings = find_swing_points(df, k=self.swing_k)
        # dédoublonnage des plateaux (cf. leçon phase 2)
        swings = self._dedup(swings)
        out: list[SwingLabel] = []
        last_high: float | None = None
        last_low: float | None = None
        for s in swings:
            if s.kind == "high":
                if last_high is None:
                    label = "H?"
                elif abs(s.price - last_high) < 1e-9:
                    label = "EQH"
                else:
                    label = "HH" if s.price > last_high else "LH"
                last_high = s.price
            else:
                if last_low is None:
                    label = "L?"
                elif abs(s.price - last_low) < 1e-9:
                    label = "EQL"
                else:
                    label = "HL" if s.price > last_low else "LL"
                last_low = s.price
            out.append(SwingLabel(s.kind, s.price, s.index, s.time, label))
        return out

    # ------------------------------------------------------------------ #
    #  2. Résumé par timeframe (tendance + labels + événements + MSS)
    # ------------------------------------------------------------------ #
    def summarize(self, df: pd.DataFrame, timeframe: str,
                  min_candles: int = 60) -> StructureSummary:
        role = "external" if timeframe in EXTERNAL_TFS else "internal"
        if df is None or len(df) < min_candles:
            return StructureSummary(timeframe, role, None, detail="données insuffisantes")

        labelled = self.label_swings(df)
        counts: dict[str, int] = {}
        for sl in labelled:
            counts[sl.label] = counts.get(sl.label, 0) + 1

        swings = self._dedup(find_swing_points(df, k=self.swing_k))
        events, state = detect_structure(df, swings, prefix="MS")
        atr = compute_atr(df, 14)

        # enrichissement : displacement sur chaque cassure + MSS
        last_mss = None
        for e in reversed(events):
            i = e["break_index"]
            body = abs(float(df["close"].iloc[i]) - float(df["open"].iloc[i]))
            a = float(atr[i]) or 1e-9
            e["body_atr"] = round(body / a, 2)
            e["is_mss"] = e["type"] == "CHoCH" and body >= self.displacement_atr * a
            if e["is_mss"] and last_mss is None:
                last_mss = {"direction": e["direction"], "break_index": i,
                            "break_time": e["break_time"], "body_atr": e["body_atr"]}

        labels_seq = [sl.label for sl in labelled[-8:]]
        trend = state.trend
        bull = counts.get("HH", 0) + counts.get("HL", 0)
        bear = counts.get("LH", 0) + counts.get("LL", 0)
        detail = (f"tendance {trend or 'indéterminée'} ; labels récents "
                  f"{' '.join(labels_seq) or '—'} ; {bull} labels haussiers / "
                  f"{bear} baissiers ; dernier événement "
                  f"{events[-1]['type'] if events else 'aucun'}")

        return StructureSummary(
            timeframe=timeframe, role=role, trend=trend,
            labels=labels_seq, counts=counts,
            last_event=dict(events[-1]) if events else None,
            last_mss=last_mss, n_swings=len(labelled), detail=detail,
        )

    # ------------------------------------------------------------------ #
    #  3. Verdict hiérarchique (externe commande, interne exécute)
    # ------------------------------------------------------------------ #
    @staticmethod
    def combine(summaries: dict[str, StructureSummary]) -> StructureVerdict:
        """La règle §10 : une confirmation M5 n'inverse jamais un D1/H4.

        externe = première tendance disponible dans l'ordre D1 > H4
        interne = première tendance disponible dans l'ordre M15 > H1 > M30 > M5
        """
        ext = next((summaries[tf].trend for tf in ("D1", "H4")
                    if tf in summaries and summaries[tf].trend), None)
        internal_order = ("M15", "H1", "M30", "M5")
        itn = next((summaries[tf].trend for tf in internal_order
                    if tf in summaries and summaries[tf].trend), None)
        insufficient = all(s.trend is None for s in summaries.values()) or not summaries

        if insufficient:
            return StructureVerdict(None, None, INSUFFICIENT, None,
                                    "données insuffisantes pour conclure")
        if ext is None:
            return StructureVerdict(None, itn, EXTERNAL_UNCLEAR, None,
                                    "structure externe (D1/H4) indéterminée — "
                                    "aucune direction autorisée")
        if itn is None:
            itn_txt = "interne indéterminée"
        else:
            itn_txt = f"interne {itn}"

        if ext == "bullish":
            if itn == "bearish":
                return StructureVerdict(
                    ext, itn, PULLBACK_UP, "LONG",
                    "D1/H4 haussier avec structure interne baissière : "
                    "pullback — chercher LONG sur reprise interne, jamais SHORT")
            return StructureVerdict(ext, itn, ALIGNED_UP, "LONG",
                                    f"externe et {itn_txt} alignés haussiers")
        if ext == "bearish":
            if itn == "bullish":
                return StructureVerdict(
                    ext, itn, PULLBACK_DOWN, "SHORT",
                    "D1/H4 baissier avec structure interne haussière : "
                    "pullback — chercher SHORT sur reprise interne, jamais LONG")
            return StructureVerdict(ext, itn, ALIGNED_DOWN, "SHORT",
                                    f"externe et {itn_txt} alignés baissiers")
        return StructureVerdict(ext, itn, EXTERNAL_UNCLEAR, None,
                                "contexte externe non directionnel")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedup(swings):
        """Fusionne les swings adjacents au même niveau (plateaux)."""
        out = []
        for s in swings:
            if out:
                last = out[-1]
                if (s.kind == last.kind and s.index - last.index <= 3
                        and abs(s.price - last.price) < 1e-9):
                    continue
            out.append(s)
        return out
