"""Anti-Overtrading Engine — PHASE 9 (module SHADOW : non branché au moteur live).

Responsabilités (spec §18, §25, §26) :

    1. DÉDOUBLONNAGE STRUCTUREL (§25 — « one setup per signal ») :
       l'identité d'un signal = (zone_id, direction). Le MÊME événement
       structurel (la même zone cassée dans le même sens) ne peut produire
       QU'UN signal, quel que soit le scénario qui l'habille (A, B, T, K...).
       Interdiction du stacking sur un seul événement.

    2. RE-ENTRY (§25) : après un SL (ou un TP), reprendre la MÊME zone dans
       le MÊME sens est interdit tant qu'une NOUVELLE structure indépendante
       n'est pas apparue :
           - nouveau sweep (après la sortie),
           - nouveau MSS/CHoCH (après la sortie),
           - nouvelle zone créée (après la sortie).

    3. COOLDOWN ASYMÉTRRIQUE : pause plus longue après une perte qu'après
       un gain (défauts : 240 min après SL, 120 min après TP, 180 min neutre).

    4. CONTRÔLES DE FRÉQUENCE (§26) : plafonds journaliers (total, par zone,
       post-SL, sens consécutif) — tous mesurables via `FrequencyMonitor`.

Codes de refus (§33) : DUPLICATE_SETUP, REENTRY_WITHOUT_NEW_STRUCTURE,
COOLDOWN_ACTIVE, DAILY_CAP, ZONE_CAP, POST_SL_CAP, CONSECUTIVE_CAP.

Fonction pure : l'historique est fourni par l'appelant (liste de
SignalRecord) — aucune dépendance au stockage réel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

# ---- codes ------------------------------------------------------------------ #
DUPLICATE_SETUP = "DUPLICATE_SETUP"
REENTRY_NO_NEW_STRUCTURE = "REENTRY_WITHOUT_NEW_STRUCTURE"
COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
DAILY_CAP = "DAILY_CAP"
ZONE_CAP = "ZONE_CAP"
POST_SL_CAP = "POST_SL_CAP"
CONSECUTIVE_CAP = "CONSECUTIVE_CAP"

EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_EXPIRE = "EXPIRE"


@dataclass(frozen=True)
class SignalRecord:
    """Un signal déjà émis (historique fourni par l'appelant)."""

    zone_id: str
    direction: str                 # LONG / SHORT
    time: pd.Timestamp
    exit_time: pd.Timestamp | None = None
    exit_kind: str | None = None   # TP / SL / EXPIRE (None = en cours)

    @property
    def identity(self) -> str:
        """Identité structurelle (§25) : zone + sens."""
        return f"{self.zone_id}:{self.direction}"


@dataclass(frozen=True)
class Candidate:
    """Candidat à l'émission, avant les contrôles."""

    zone_id: str
    direction: str
    scenario: str = ""

    @property
    def identity(self) -> str:
        return f"{self.zone_id}:{self.direction}"


@dataclass
class ContextEvents:
    """Événements structurels RÉCENTS, pour juger d'une nouvelle structure."""

    sweep_times: list[pd.Timestamp] = field(default_factory=list)
    break_times: list[pd.Timestamp] = field(default_factory=list)    # MSS/CHoCH/BOS
    zone_created_times: list[pd.Timestamp] = field(default_factory=list)

    def new_structure_since(self, since: pd.Timestamp) -> list[str]:
        """Structures indépendantes apparues APRÈS `since` (§25)."""
        out = []
        for t in self.sweep_times:
            if t > since:
                out.append(f"sweep @ {t}")
        for t in self.break_times:
            if t > since:
                out.append(f"cassure de structure @ {t}")
        for t in self.zone_created_times:
            if t > since:
                out.append(f"zone créée @ {t}")
        return out


@dataclass
class OvertradeDecision:
    allowed: bool
    code: str                      # "" si autorisé, sinon code de refus
    detail: str = ""


class AntiOvertradingEngine:
    """Garde-fous anti-surtrading : dedup, re-entry, cooldowns, plafonds."""

    def __init__(
        self,
        cooldown_neutral_min: int = 180,
        cooldown_after_tp_min: int = 120,
        cooldown_after_sl_min: int = 240,   # asymétrique : la perte paie plus
        max_per_day: int = 3,
        max_same_zone_per_day: int = 2,
        max_post_sl_per_day: int = 2,
        max_consecutive_same_direction: int = 3,
    ) -> None:
        self.cooldown_neutral = pd.Timedelta(minutes=cooldown_neutral_min)
        self.cooldown_after_tp = pd.Timedelta(minutes=cooldown_after_tp_min)
        self.cooldown_after_sl = pd.Timedelta(minutes=cooldown_after_sl_min)
        self.max_per_day = int(max_per_day)
        self.max_same_zone_per_day = int(max_same_zone_per_day)
        self.max_post_sl_per_day = int(max_post_sl_per_day)
        self.max_consecutive = int(max_consecutive_same_direction)

    # ------------------------------------------------------------------ #
    def check(
        self,
        candidate: Candidate,
        history: list[SignalRecord],
        now: pd.Timestamp,
        context: ContextEvents | None = None,
    ) -> OvertradeDecision:
        """Exécute TOUS les garde-fous, dans l'ordre du plus fondamental."""
        today = now.normalize()

        # --- 1. Déduplication structurelle (§25) ---------------------------
        # Même identité (zone + sens) : interdit, SAUF si une nouvelle
        # structure indépendante est apparue depuis le dernier signal sur
        # cette identité (§25 : la re-entry exige une nouvelle justification).
        same = [h for h in history if h.identity == candidate.identity]
        if same:
            last = max(same, key=lambda h: h.time)
            ctx0 = context or ContextEvents()
            if not ctx0.new_structure_since(last.time):
                return OvertradeDecision(
                    False, DUPLICATE_SETUP,
                    f"la zone {candidate.zone_id} a déjà produit un signal "
                    f"{candidate.direction} à {last.time:%d %b %H:%M} sans nouvelle "
                    "structure depuis — un événement structurel = un signal "
                    "maximum (anti-stacking §25)")

        # --- 2. Cooldown basé sur la DERNIÈRE sortie -------------------------
        closed = [h for h in history if h.exit_time is not None]
        if closed:
            last_exit = max(closed, key=lambda h: h.exit_time)
            if last_exit.exit_kind == EXIT_SL:
                cooldown = self.cooldown_after_sl
            elif last_exit.exit_kind == EXIT_TP:
                cooldown = self.cooldown_after_tp
            else:
                cooldown = self.cooldown_neutral
            elapsed = now - last_exit.exit_time
            if elapsed < cooldown:
                remaining = int((cooldown - elapsed).total_seconds() // 60)
                total = int(cooldown.total_seconds() // 60)
                return OvertradeDecision(
                    False, COOLDOWN_ACTIVE,
                    f"cooldown post-{last_exit.exit_kind} : {remaining} min restantes "
                    f"sur {total} (asymétrique : perte = pause plus longue)")

        # --- 3. Re-entry sur la même zone+sens opposé déjà tenté ? -----------
        # (le dedup couvre le même sens ; ici : la zone a-t-elle un historique
        #  de SORTIE récente quelle que soit la direction ? Si la dernière
        #  sortie de CETTE zone est un SL, exiger une nouvelle structure.)
        zone_history = [h for h in history if h.zone_id == candidate.zone_id
                        and h.exit_time is not None]
        if zone_history:
            last_zone_exit = max(zone_history, key=lambda h: h.exit_time)
            if last_zone_exit.exit_kind in (EXIT_SL, EXIT_EXPIRE):
                ctx = context or ContextEvents()
                news = ctx.new_structure_since(last_zone_exit.exit_time)
                if not news:
                    return OvertradeDecision(
                        False, REENTRY_NO_NEW_STRUCTURE,
                        f"sortie {last_zone_exit.exit_kind} sur {candidate.zone_id} "
                        f"à {last_zone_exit.exit_time:%d %b %H:%M} sans nouvelle "
                        "structure indépendante depuis (re-entry interdit §25)")

        # --- 4. Plafonds journaliers (§26) ------------------------------------
        today_signals = [h for h in history if h.time >= today]
        if len(today_signals) >= self.max_per_day:
            return OvertradeDecision(
                False, DAILY_CAP,
                f"{len(today_signals)} signaux déjà émis aujourd'hui "
                f"(max {self.max_per_day})")

        zone_today = [h for h in today_signals if h.zone_id == candidate.zone_id]
        if len(zone_today) >= self.max_same_zone_per_day:
            return OvertradeDecision(
                False, ZONE_CAP,
                f"{len(zone_today)} signaux sur la zone {candidate.zone_id} "
                f"aujourd'hui (max {self.max_same_zone_per_day})")

        # --- 5. Plafond post-SL (§26 : signaux après SL) ------------------------
        post_sl_today = 0
        for h in sorted(history, key=lambda x: x.time):
            if h.time >= today and h.exit_kind == EXIT_SL and h.exit_time is not None:
                # les signaux émis après CETTE perte
                post_sl_today = sum(
                    1 for g in history if h.exit_time < g.time <= now)
        if post_sl_today >= self.max_post_sl_per_day:
            return OvertradeDecision(
                False, POST_SL_CAP,
                f"{post_sl_today} signaux déjà émis après des pertes aujourd'hui "
                f"(max {self.max_post_sl_per_day})")

        # --- 6. Sens consécutifs (§26) ------------------------------------------
        ordered = sorted(history, key=lambda h: h.time)
        streak = 0
        for h in reversed(ordered):
            if h.direction == candidate.direction:
                streak += 1
            else:
                break
        if streak >= self.max_consecutive:
            return OvertradeDecision(
                False, CONSECUTIVE_CAP,
                f"{streak} signaux {candidate.direction} consécutifs "
                f"(max {self.max_consecutive}) — diversification/réévaluation requise")

        return OvertradeDecision(True, "", "tous les garde-fous passés")


class FrequencyMonitor:
    """Mesure des fréquences (§26) — pour l'analyse, pas pour bloquer."""

    @staticmethod
    def stats(history: list[SignalRecord], now: pd.Timestamp) -> dict:
        today = now.normalize()
        today_signals = [h for h in history if h.time >= today]
        last_sl = [h for h in history if h.exit_kind == EXIT_SL]
        return {
            "total": len(history),
            "today": len(today_signals),
            "zones_today": len({h.zone_id for h in today_signals}),
            "by_direction": {
                d: sum(1 for h in history if h.direction == d)
                for d in ("LONG", "SHORT")},
            "sl_count": len(last_sl),
            "tp_count": sum(1 for h in history if h.exit_kind == EXIT_TP),
            "open": sum(1 for h in history if h.exit_time is None),
        }
