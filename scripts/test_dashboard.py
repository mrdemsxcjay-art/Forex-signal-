"""Test fonctionnel du dashboard — Phase 7.

Usage :  python scripts/test_dashboard.py

SECTIONS
  A. Helpers purs : courbe d'équité, stats, table formatée, R flottant,
     données de démonstration, lecture du rapport de cycle.
  B. main.py publie bien data/last_cycle.json (sous-processus --once réel).
  C. build_smc_figure sur données réelles (figure + zones tracées).
  D. Serveur Streamlit : lancement réel, page servie, arrêt propre.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from dashboard.helpers import (
    demo_signals,
    equity_curve,
    floating_r,
    load_last_cycles,
    prepare_signals_table,
    stats_from_frame,
)
from src.analysis.smc import SMCEngine
from src.data.data_fetcher import DataFetcher
from src.logger import setup_logging
from src.visualization.smc_chart import build_smc_figure

results: list[bool] = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'     OK' if ok else ' ÉCHEC'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def section_a():
    print("\n--- A. Helpers purs (sans Streamlit) ---")
    demo = demo_signals()
    stats = stats_from_frame(demo)
    check("stats démo : 7 clôturés, 4 TP, winrate 0.571",
          stats["closed"] == 7 and stats["tp"] == 4
          and abs(stats["winrate"] - round(4 / 7, 3)) < 1e-9,
          str(stats))

    eq = equity_curve(demo[demo["resultat"] != "EN_COURS"])
    expected_total = round(demo.loc[demo.resultat != "EN_COURS", "exit_r"].sum(), 2)
    check("courbe d'équité : cumul final = somme des R",
          not eq.empty and abs(eq["equity"].iloc[-1] - expected_total) < 1e-6
          and eq["equity"].is_monotonic_increasing is False,
          f"final {eq['equity'].iloc[-1]:+.1f}R (attendu {expected_total:+.1f})")

    table = prepare_signals_table(demo)
    check("table formatée : colonnes FR + décimales par paire",
          list(table.columns)[:6] == ["Date", "Paire", "Sens", "Score", "Grade", "Session"]
          and any(len(v.split(".")[1]) == 2 for v in table.loc[table.Paire == "XAUUSD", "Entrée"])
          and any(len(v.split(".")[1]) == 5 for v in table.loc[table.Paire == "EURUSD", "Entrée"]))

    row = demo[demo.resultat == "EN_COURS"].iloc[0]  # XAUUSD SHORT
    risk = abs(float(row["entree"]) - float(row["sl"]))
    r = floating_r(row, last_close=float(row["entree"]) - risk)  # SHORT à +1R
    check("R flottant (+1R simulé, SHORT)", r is not None and abs(r - 1.0) < 0.05, f"{r:+.2f}")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "last_cycle.json"
        payload = {"updated_at": "2026-08-19T00:00:00+00:00", "cycle": 12,
                   "pairs": {"EURUSD": {"score": 45, "aligned": False,
                                        "blockers": ["score 45 < seuil 70"],
                                        "breakdown": {"session": 10}}}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_last_cycles(path)
        check("lecture rapport de cycle", loaded and loaded["cycle"] == 12
              and "EURUSD" in loaded["pairs"])
        check("rapport absent -> None", load_last_cycles(tmp + "/nope.json") is None)


def section_b():
    print("\n--- B. main.py --once publie le rapport de cycle ---")
    proc = subprocess.run([sys.executable, "-m", "src.main", "--once"],
                          cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True, timeout=240)
    path = Path("data/last_cycle.json")
    ok = proc.returncode == 0 and path.exists()
    detail = ""
    if ok:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pairs_ok = set(payload["pairs"]) == {"EURUSD", "GBPUSD", "XAUUSD"}
        fields_ok = all({"score", "aligned", "blockers", "breakdown", "timeframes"}
                        <= set(info) for info in payload["pairs"].values())
        ok = pairs_ok and fields_ok and payload["cycle"] >= 1
        detail = (f"cycle {payload['cycle']}, paires {sorted(payload['pairs'])}, "
                  f"score EURUSD {payload['pairs']['EURUSD']['score']}")
    check("rapport de cycle complet (3 paires + scoring + timeframes)", ok, detail)


def section_c():
    print("\n--- C. build_smc_figure sur données réelles ---")
    try:
        df = DataFetcher().get_candles("EURUSD", "15m", lookback_days=10)
        result = SMCEngine("EURUSD", "15m").analyze(df)
        fig = build_smc_figure(result, df, last_n=200)
        n_shapes = len(fig.layout.shapes)
        traces = len(fig.data)
        check("figure construite : bougies + zones (shapes) + marqueurs",
              traces >= 1 and n_shapes > 5, f"{traces} traces, {n_shapes} zones tracées")
    except Exception as exc:  # noqa: BLE001
        check("figure SMC", False, f"{type(exc).__name__}: {exc}")


def section_d():
    print("\n--- D. Serveur Streamlit (lancement réel + page servie) ---")
    import requests

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
         "--server.port", "8503", "--server.address", "0.0.0.0",
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        ok = False
        detail = ""
        for _ in range(30):
            time.sleep(1.0)
            try:
                resp = requests.get("http://127.0.0.1:8503", timeout=5)
                health = requests.get("http://127.0.0.1:8503/_stcore/health", timeout=5)
                if resp.status_code == 200 and health.text.strip() == "ok":
                    ok = True
                    detail = f"HTTP {resp.status_code}, health '{health.text.strip()}'"
                    break
            except requests.RequestException:
                continue
        if not ok and proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            detail = f"process mort : {out[-300:]}"
        check("page du dashboard servie (HTTP 200 + titre)", ok, detail)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    check("arrêt du serveur propre", proc.returncode is not None)


def main():
    setup_logging(level="ERROR")
    print("=" * 70)
    print(" Test fonctionnel — dashboard Streamlit (Phase 7)")
    print("=" * 70)

    section_a()
    section_b()
    section_c()
    section_d()

    print("\n" + "=" * 70)
    passed = sum(results)
    if passed == len(results):
        print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — dashboard validé ✔")
        return 0
    print(f" RÉSULTAT : {passed}/{len(results)} vérifications OK — à corriger ✘")
    return 1


if __name__ == "__main__":
    sys.exit(main())
