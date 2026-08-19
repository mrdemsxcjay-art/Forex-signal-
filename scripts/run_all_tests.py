"""Exécute TOUTE la suite de tests du projet et synthétise les résultats.

Usage :  python scripts/run_all_tests.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SUITES = [
    ("verify_install", ["scripts/verify_install.py"], 300),
    ("test_data", ["scripts/test_data.py"], 600),
    ("test_smc", ["scripts/test_smc.py"], 600),
    ("test_signals", ["scripts/test_signals.py"], 600),
    ("test_loop", ["scripts/test_loop.py"], 900),
    ("test_dashboard", ["scripts/test_dashboard.py"], 900),
]

PATTERN = re.compile(r"RÉSULTAT\s*:\s*(\d+)/(\d+)")


def main() -> int:
    print("=" * 70)
    print(" SUITE COMPLÈTE — Forex Signals SMC")
    print("=" * 70)
    summary: list[tuple[str, int, int, bool]] = []

    for name, script, timeout in SUITES:
        print(f"\n>>> {name} ({script[0]})")
        try:
            proc = subprocess.run(
                [sys.executable, *script], cwd=ROOT,
                capture_output=True, text=True, timeout=timeout,
            )
            output = proc.stdout + proc.stderr
            match = PATTERN.search(output)
            if match:
                passed, total = int(match.group(1)), int(match.group(2))
                summary.append((name, passed, total, passed == total and proc.returncode == 0))
            else:
                # Repli (ex. verify_install) : compte des lignes [OK]/[ÉCHEC]
                ok_lines = len(re.findall(r"\[OK", output))
                fail_lines = len(re.findall(r"\[ÉCHEC\]", output))
                summary.append((name, ok_lines, ok_lines + fail_lines,
                                proc.returncode == 0 and fail_lines == 0))
            tail = [l for l in output.splitlines() if l.strip()][-1]
            print(f"    {tail}")
        except subprocess.TimeoutExpired:
            summary.append((name, 0, 1, False))
            print("    TIMEOUT")
        except Exception as exc:  # noqa: BLE001
            summary.append((name, 0, 1, False))
            print(f"    ERREUR : {exc}")

    print("\n" + "=" * 70)
    print(" SYNTHÈSE")
    print("=" * 70)
    all_ok = True
    total_passed = total_checks = 0
    for name, passed, total, ok in summary:
        state = "OK " if ok else "ÉCHEC"
        print(f"  [{state}] {name:<15} {passed}/{total}")
        all_ok &= ok
        total_passed += passed
        total_checks += total
    print("-" * 70)
    print(f"  TOTAL : {total_passed}/{total_checks} — "
          + ("PROJET VALIDÉ ✔" if all_ok else "À CORRIGER ✘"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
