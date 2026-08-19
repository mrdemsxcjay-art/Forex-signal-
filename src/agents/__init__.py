"""Agents spécialisés : fondamental (Agent 1), SMC (Agent 2), risque (Agent 3).

Chaque agent a UNE responsabilité et rend un verdict structuré (dataclass).
Le moteur de signaux (src/signals/engine.py) les orchestre : aucun agent
n'envoie de message, n'écrit en base ou ne déclenche quoi que ce soit —
séparation stricte analyse / décision / action.
"""
