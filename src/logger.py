"""Journalisation centrale de l'application.

Choix techniques :
- Module standard ``logging`` : aucune dépendance, éprouvé.
- Deux sorties  : console (niveau configurable) + fichier (tout, en DEBUG).
- Rotation      : RotatingFileHandler -> 5 fichiers de 5 Mo max, on ne sature
                  jamais le disque même après des mois d'exécution.

Idempotent : appeler ``setup_logging()`` plusieurs fois ne duplique pas
les handlers (important avec Streamlit qui recharge les modules).
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """Initialise la journalisation et renvoie le logger racine.

    Args:
        level:        niveau console (DEBUG / INFO / WARNING / ERROR).
        log_file:     chemin du fichier de log (créé si besoin, parents inclus).
        max_bytes:    taille max d'un fichier avant rotation.
        backup_count: nombre de fichiers d'archive conservés.
    """
    root = logging.getLogger()

    # Nettoyage préalable -> idempotence
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 1) Console : lisible, niveau demandé
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(formatter)
    root.addHandler(console)

    # 2) Fichier tournant : tout est conservé (DEBUG) pour l'audit a posteriori
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    return root
