"""Notifications Telegram — Bot API via requêtes HTTP simples (gratuit).

Format PROFESSIONNEL SANS EMOJI : séparateurs à filets (━), coches
typographiques (✓ U+2713, caractère de texte et non un emoji), aucune
image. Trois familles de messages :
    - carte de signal (format de la spécification)
    - clôture de signal (issue TP/SL/EXPIRE + R réalisé + solde cumulé)
    - messages opérationnels (heartbeat, rétablissement de source)

Robustesse : 3 tentatives avec backoff ; si le token/chat_id ne sont pas
configurés (.env absent), l'envoi est sauté avec un WARNING — le moteur
continue de tourner et d'écrire en base (dégradation gracieuse).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from ..signals.models import Signal, pip_spec

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_price(pair: str, value: float) -> str:
    pip_size, _ = pip_spec(pair)
    decimals = 5 if pip_size <= 0.001 else 3 if pip_size <= 0.05 else 2
    return f"{value:.{decimals}f}"


def format_signal_message(signal: Signal) -> str:
    """Carte de signal texte, format professionnel sans emoji."""
    pair_disp = f"{signal.pair[:3]}/{signal.pair[3:]}"
    line = "━" * 24
    parts = [
        line,
        "SIGNAL DETECTE",
        line,
        f"Paire       : {pair_disp}",
        f"Direction   : {signal.direction}",
        f"Session     : {signal.session}",
        line,
        f"Entree      : {_fmt_price(signal.pair, signal.risk.entry)}",
        f"TP          : {_fmt_price(signal.pair, signal.risk.tp)} "
        f"(+{signal.risk.tp_pips:.0f} pips)",
        f"SL          : {_fmt_price(signal.pair, signal.risk.sl)} "
        f"(-{signal.risk.risk_pips:.0f} pips)",
        f"R/R         : 1:{signal.risk.rr:.1f}",
        f"Taille      : {signal.risk.lots} lots",
        line,
        "Confluences detectees :",
        *(f"  ✓ {c}" for c in signal.confluences),
        line,
        f"Score de confiance : {signal.score}/100 (grade {signal.grade})",
        f"Date : {signal.created_at}",
        line,
        "Trading = risque. DYOR.",
    ]
    return "\n".join(parts)


def format_closure_message(resolution: dict, signal_row, stats: dict) -> str:
    """Message de clôture : issue, R réalisé, solde cumulé du compte de signaux."""
    pair = str(resolution["paire"])
    pair_disp = f"{pair[:3]}/{pair[3:]}"
    line = "━" * 24
    outcome_label = {
        "TP_ATTEINT": "OBJECTIF ATTEINT",
        "SL_ATTEINT": "STOP ATTEINT",
        "EXPIRE": "EXPIRATION",
    }.get(resolution["resultat"], resolution["resultat"])
    closed = stats.get("closed") or 0
    winrate = stats.get("winrate")
    winrate_txt = f"{winrate * 100:.0f} %" if winrate is not None else "n/a"
    parts = [
        line,
        "SIGNAL CLOTURE",
        line,
        f"Paire   : {pair_disp} {resolution['type']} (#{resolution['id']})",
        f"Issue   : {outcome_label}",
        f"Entree  : {_fmt_price(pair, float(signal_row['entree']))}",
        f"Sortie  : {_fmt_price(pair, float(resolution['exit_price']))}",
        f"Resultat: {resolution['exit_r']:+.1f}R",
        line,
        f"Solde cumule : {stats.get('total_r', 0.0):+.1f}R sur {closed} clôture(s)",
        f"Winrate      : {winrate_txt}",
        line,
        "Trading = risque. DYOR.",
    ]
    return "\n".join(parts)


def format_heartbeat(started: bool, info: dict) -> str:
    """Message de vie du process (démarrage / arrêt propre)."""
    line = "━" * 24
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if started:
        body = [
            line, "MOTEUR DE SIGNAUX — DEMARRAGE", line,
            f"Version   : {info.get('version', '?')}",
            f"Paires    : {', '.join(info.get('pairs', []))}",
            f"Cycle     : {info.get('interval', '?')} s",
            f"Seuil     : {info.get('threshold', '?')}/100",
            f"Heure     : {now}",
            line, "Analyse uniquement — aucun ordre n'est execute. DYOR.",
        ]
    else:
        body = [
            line, "MOTEUR DE SIGNAUX — ARRET PROPRE", line,
            f"Cycles         : {info.get('cycles', 0)}",
            f"Signaux emis   : {info.get('signals', 0)}",
            f"Clotures       : {info.get('resolutions', 0)}",
            f"Erreurs isolees: {info.get('errors', 0)}",
            f"Duree          : {info.get('uptime', '?')}",
            f"Heure          : {now}",
            line,
        ]
    return "\n".join(body)


class TelegramSender:
    """Envoi des signaux via la Bot API Telegram."""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True,
                 timeout: int = 15, max_retries: int = 3) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self.timeout = timeout
        self.max_retries = max_retries

    @classmethod
    def from_config(cls, cfg) -> "TelegramSender":
        return cls(cfg.telegram.bot_token, cfg.telegram.chat_id, cfg.telegram.enabled)

    # ------------------------------------------------------------------ #
    def send_text(self, text: str) -> bool:
        """Envoie un message texte brut. True si livré."""
        if not self.enabled:
            logger.warning("Telegram non configuré (TELEGRAM_BOT_TOKEN/CHAT_ID) — envoi ignoré")
            return False
        url = API_URL.format(token=self.bot_token)
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                if resp.ok and resp.json().get("ok"):
                    return True
                logger.warning("Telegram HTTP %s : %s", resp.status_code, resp.text[:120])
            except requests.RequestException as exc:
                logger.warning("Telegram tentative %d/%d échouée : %s", attempt, self.max_retries, exc)
            time.sleep(1.0 * attempt)
        logger.error("Telegram : envoi abandonné après %d tentatives", self.max_retries)
        return False

    def send_signal(self, signal: Signal) -> bool:
        """Formate et envoie la carte de signal."""
        return self.send_text(format_signal_message(signal))
