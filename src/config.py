"""Chargement de la configuration de l'application.

Deux sources, bien séparées :

1. ``config/settings.yaml``  -> paramètres NON sensibles, versionnables dans Git
2. ``.env``                  -> SECRETS (token Telegram), jamais versionnés

Exposé sous forme de dataclasses immuables : une erreur de frappe dans le YAML
est détectée immédiatement au démarrage, pas au milieu d'une nuit de trading.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# --- Racine du projet : forex-signals/ peu importe d'où on lance le code ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.yaml"
ENV_FILE = PROJECT_ROOT / ".env"


def _opt_int(section: dict, key: str) -> int | None:
    value = section.get(key)
    return int(value) if value is not None else None


def _opt_float(section: dict, key: str) -> float | None:
    value = section.get(key)
    return float(value) if value is not None else None


# --------------------------------------------------------------------------- #
#  Modèles de configuration (structure du settings.yaml)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TradingConfig:
    pairs: list[str]
    timeframes: dict[str, str]
    poll_interval_seconds: int
    lookback_days: int


@dataclass(frozen=True)
class DataConfig:
    provider: str
    min_request_interval_seconds: float
    max_retries: int
    retry_base_delay_seconds: float
    max_candles: int


@dataclass(frozen=True)
class GradeConfig:
    min: float
    label: str


@dataclass(frozen=True)
class SignalsConfig:
    min_score: float
    max_per_pair_per_day: int
    grades: list[GradeConfig] = field(default_factory=list)
    # Paramètres moteur (None = valeur par défaut du code)
    trigger_max_age_candles: int | None = None
    zone_proximity_atr: float | None = None
    min_rr: float | None = None
    default_rr: float | None = None
    risk_pct: float | None = None
    account_size: float | None = None
    cooldown_minutes: int | None = None


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool
    high_impact_only: bool
    block_minutes_before: int
    block_minutes_after: int


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: str
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class DashboardConfig:
    port: int


@dataclass(frozen=True)
class Config:
    app_name: str
    timezone: str
    trading: TradingConfig
    data: DataConfig
    signals: SignalsConfig
    news: NewsConfig
    telegram: TelegramConfig
    logging: LoggingConfig
    dashboard: DashboardConfig

    @property
    def log_file_path(self) -> Path:
        """Chemin absolu du fichier de log (résolu depuis la racine)."""
        return PROJECT_ROOT / self.logging.file


# --------------------------------------------------------------------------- #
#  Chargement
# --------------------------------------------------------------------------- #
def load_config(settings_file: Path = SETTINGS_FILE) -> Config:
    """Charge YAML + .env et renvoie un objet ``Config`` validé.

    Raises:
        FileNotFoundError : settings.yaml introuvable.
        KeyError / ValueError : clé manquante ou valeur incohérente.
    """
    if not settings_file.exists():
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {settings_file}"
        )

    # Les variables du .env arrivent dans os.environ (sans écraser l'existant)
    load_dotenv(ENV_FILE)

    raw = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    try:
        app = raw["app"]
        trading = raw["trading"]
        signals = raw["signals"]
        news = raw["news"]
        logging_ = raw["logging"]
        dashboard = raw["dashboard"]
    except KeyError as exc:
        raise KeyError(f"Section manquante dans {settings_file.name} : {exc}") from exc

    data = raw.get("data", {})  # section optionnelle -> valeurs par défaut
    data_cfg = DataConfig(
        provider=str(data.get("provider", "yahoo")),
        min_request_interval_seconds=float(data.get("min_request_interval_seconds", 0.5)),
        max_retries=int(data.get("max_retries", 4)),
        retry_base_delay_seconds=float(data.get("retry_base_delay_seconds", 1.0)),
        max_candles=int(data.get("max_candles", 3000)),
    )

    grades = [
        GradeConfig(min=float(g["min"]), label=str(g["label"]))
        for g in signals.get("grades", [])
    ]

    telegram_enabled = str(
        os.getenv("TELEGRAM_ENABLED", raw.get("telegram", {}).get("enabled", True))
    ).lower() in ("1", "true", "yes", "on")

    cfg = Config(
        app_name=str(app["name"]),
        timezone=str(app.get("timezone", "UTC")),
        trading=TradingConfig(
            pairs=[str(p) for p in trading["pairs"]],
            timeframes=dict(trading["timeframes"]),
            poll_interval_seconds=int(trading["poll_interval_seconds"]),
            lookback_days=int(trading["lookback_days"]),
        ),
        data=data_cfg,
        signals=SignalsConfig(
            min_score=float(signals["min_score"]),
            max_per_pair_per_day=int(signals["max_per_pair_per_day"]),
            grades=grades,
            trigger_max_age_candles=_opt_int(signals, "trigger_max_age_candles"),
            zone_proximity_atr=_opt_float(signals, "zone_proximity_atr"),
            min_rr=_opt_float(signals, "min_rr"),
            default_rr=_opt_float(signals, "default_rr"),
            risk_pct=_opt_float(signals, "risk_pct"),
            account_size=_opt_float(signals, "account_size"),
            cooldown_minutes=_opt_int(signals, "cooldown_minutes"),
        ),
        news=NewsConfig(
            enabled=bool(news["enabled"]),
            high_impact_only=bool(news["high_impact_only"]),
            block_minutes_before=int(news["block_minutes_before"]),
            block_minutes_after=int(news["block_minutes_after"]),
        ),
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            enabled=telegram_enabled,
        ),
        logging=LoggingConfig(
            level=str(logging_["level"]),
            file=str(logging_["file"]),
            max_bytes=int(logging_["max_bytes"]),
            backup_count=int(logging_["backup_count"]),
        ),
        dashboard=DashboardConfig(port=int(dashboard["port"])),
    )

    # Validation rapide des incohérences classiques
    if not 0 <= cfg.signals.min_score <= 100:
        raise ValueError("signals.min_score doit être entre 0 et 100 (score /100).")
    if not cfg.trading.pairs:
        raise ValueError("trading.pairs ne peut pas être vide.")

    return cfg
