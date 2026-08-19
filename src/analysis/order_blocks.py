"""Order Blocks (blocs d'ordres) — origine des mouvements impulsifs.

DÉFINITION (ICT) :
    Order Block haussier = la DERNIÈRE bougie baissière (close < open) qui
    précède le mouvement impulsif À LA HAUSSE qui casse la structure (BOS/CHoCH
    haussier). Zone = étendue de cette bougie (mèches comprises par défaut).
    Miroir pour l'OB baissier.

LOGIQUE ÉCONOMIQUE : cette bougie baissière est l'endroit où les institutions
ont accumulé leurs positions (les vendeurs y ont été absorbés) ; lorsque le
prix y revient, ces ordres non exécutés/les positions ouvertes réagissent ->
zone de réaction acheteuse.

ANTI-REPAINT :
    - l'OB n'existe qu'à partir de la CLÔTURE de la bougie de cassure
      (confirmed_time) : avant, le "mouvement impulsif" n'est pas un fait ;
    - la zone (origin candle) est une donnée historique figée ;
    - son état évolue par AJOUT uniquement : touched (premier retour du prix),
      invalidated (clôture au-delà du bord opposé). Une zone invalidée reste
      visible (grisée) : c'est de l'information, pas du repaint.

INVALIDATION :
    OB haussier invalidé si une bougie CLÔTURE sous zone_bottom (les mèches
    qui percent puis reviennent = mitigation classique, on note touched).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def detect_order_blocks(
    df: pd.DataFrame,
    structure_events: list[dict],
    max_lookback: int = 20,
    use_body: bool = False,
) -> list[dict]:
    """Un OB par événement de cassure (BOS/CHoCH), statuts consolidés ensuite."""
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    times = df.index
    n = len(df)

    blocks: list[dict] = []

    for ev in structure_events:
        i = ev["break_index"]
        bullish = ev["direction"] == "bullish"
        lo_bound = max(0, i - max_lookback)

        origin = None
        if bullish:
            # dernière bougie baissière avant la cassure haussière
            j = i - 1
            while j > lo_bound and closes[j] >= opens[j]:
                j -= 1
            if closes[j] < opens[j]:
                origin = j
            else:  # pas de bougie baissière dans la fenêtre -> plus bas de la jambe
                origin = int(np.argmin(lows[lo_bound:i + 1])) + lo_bound
        else:
            # dernière bougie haussière avant la cassure baissière
            j = i - 1
            while j > lo_bound and closes[j] <= opens[j]:
                j -= 1
            if closes[j] > opens[j]:
                origin = j
            else:
                origin = int(np.argmax(highs[lo_bound:i + 1])) + lo_bound

        if use_body:
            zone_bottom = float(min(opens[origin], closes[origin]))
            zone_top = float(max(opens[origin], closes[origin]))
        else:
            zone_bottom = float(lows[origin])
            zone_top = float(highs[origin])

        blocks.append({
            "id": f"OB-{len(blocks) + 1:04d}",
            "direction": ev["direction"],
            "zone_top": round(zone_top, 6),
            "zone_bottom": round(zone_bottom, 6),
            "origin_time": times[origin],
            "confirmed_time": ev["break_time"],  # = clôture de la cassure
            "origin_index": int(origin),
            "confirm_index": int(i),
            "break_event_id": ev["id"],
            "status": "active",
            "touched_at": None,
            "invalidated_at": None,
        })

    # Passe chronologique : touché / invalidé (par AJOUT, jamais de retrait)
    for ob in blocks:
        start = ob["confirm_index"] + 1
        for i in range(start, n):
            if ob["direction"] == "bullish":
                if closes[i] < ob["zone_bottom"]:
                    ob["status"] = "invalidated"
                    ob["invalidated_at"] = times[i]
                    break
                if ob["touched_at"] is None and lows[i] <= ob["zone_top"]:
                    ob["touched_at"] = times[i]
            else:
                if closes[i] > ob["zone_top"]:
                    ob["status"] = "invalidated"
                    ob["invalidated_at"] = times[i]
                    break
                if ob["touched_at"] is None and highs[i] >= ob["zone_bottom"]:
                    ob["touched_at"] = times[i]

    return blocks
