"""Modèles de données des signaux (contrat entre agents, moteur et sorties)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


#: Taille d'un pip et valeur du pip par lot standard (indicatif, 100k unités).
PIP_SPECS: dict[str, tuple[float, float]] = {
    "EURUSD": (0.0001, 10.0),
    "GBPUSD": (0.0001, 10.0),
    "AUDUSD": (0.0001, 10.0),
    "USDCAD": (0.0001, 7.3),  # 10 CAD convertis (indicatif)
    "USDJPY": (0.01, 6.6),    # 1000 JPY convertis (indicatif)
    "XAUUSD": (0.10, 10.0),   # 1 lot = 100 oz -> 0.1 $/pip de 0.1
}
DEFAULT_PIP_SPEC = (0.0001, 10.0)


def pip_spec(pair: str) -> tuple[float, float]:
    return PIP_SPECS.get(pair.upper(), DEFAULT_PIP_SPEC)


@dataclass(frozen=True)
class FundamentalView:
    """Verdict de l'Agent 1 (fondamental) — ne regarde QUE les news."""

    bias: str                      # BULLISH / BEARISH / NEUTRAL (biais de la paire)
    score: float                   # force du biais (avec seuil en tête)
    supports_direction: bool       # le biais soutient-il la direction candidate ?
    drivers: list[str] = field(default_factory=list)
    high_impact_soon: bool = False # news rouge imminente (Agent 3 décide)


@dataclass(frozen=True)
class MultiTFView:
    """Verdict de l'Agent 2 (SMC) — ne regarde QUE les graphiques.

    Trois timeframes, trois rôles (méthode top-down) :
        D1  -> biais directionnel (on ne trade JAMAIS contre D1)
        H4  -> zones d'intérêt (order blocks, FVG, liquidité)
        M15 -> déclencheur d'entrée (CHoCH/BOS récent + retour en zone)
    """

    d1_bias: str                            # bullish / bearish / neutral
    d1_event: str | None                    # dernier événement D1 (BOS/CHoCH)
    h4_supports: bool                       # le H4 soutient-il la direction ?
    h4_reason: str                          # libellé lisible
    m15_trigger: str | None                 # bullish / bearish / None
    m15_trigger_kind: str | None            # "CHoCH" / "BOS"
    m15_trigger_age: int | None             # bougies depuis le déclencheur
    current_price: float
    atr_m15: float
    ob_near: dict | None                    # zone OB actionnable (direction)
    fvg_near: dict | None                   # FVG actionnable (direction)
    sweep_recent: dict | None               # sweep récent aligné
    premium_discount: str | None            # premium / discount / equilibrium
    pd_position_pct: float | None           # position du prix dans le range (0-100)
    direction: str | None                   # candidat LONG/SHORT (ou None)


@dataclass(frozen=True)
class RiskPlan:
    """Verdict de l'Agent 3 (risque) : validation + plan de trade chiffré."""

    valid: bool
    entry: float
    sl: float
    tp: float
    rr: float                     # reward/risk (ex. 2.0 = 1:2)
    risk_pips: float
    tp_pips: float
    lots: float                   # suggestion pour risk_pct du compte
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass
class Signal:
    """Signal complet prêt pour Telegram / SQLite / carte SVG."""

    pair: str
    direction: str                # LONG / SHORT
    score: int
    grade: str                    # A+ / A / B
    session: str
    risk: RiskPlan
    confluences: list[str]
    breakdown: dict               # détail du score par composante
    timeframes: dict              # {D1:..., H4:..., M15:...}
    fundamental: dict
    created_at: str = field(default_factory=utc_now_iso)
    db_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "score": self.score,
            "grade": self.grade,
            "session": self.session,
            "entry": self.risk.entry,
            "sl": self.risk.sl,
            "tp": self.risk.tp,
            "rr": self.risk.rr,
            "risk_pips": self.risk.risk_pips,
            "tp_pips": self.risk.tp_pips,
            "lots": self.risk.lots,
            "confluences": self.confluences,
            "breakdown": self.breakdown,
            "timeframes": self.timeframes,
            "fundamental": self.fundamental,
            "created_at": self.created_at,
            "db_id": self.db_id,
        }


@dataclass
class CycleReport:
    """Traçabilité complète d'un cycle d'analyse (même sans signal émis).

    Indispensable pour le dashboard : montrer POURQUOI un signal n'est pas
    parti (désalignement, score insuffisant, news, cooldown...).
    """

    pair: str
    generated_at: str
    candidate_direction: str | None
    aligned: bool
    alignment_detail: dict
    score: int
    breakdown: dict
    passed_threshold: bool
    risk_valid: bool
    blockers: list[str]
    signal: Signal | None = None
    stored: bool = False
    telegram_sent: bool = False
    card_path: str | None = None
