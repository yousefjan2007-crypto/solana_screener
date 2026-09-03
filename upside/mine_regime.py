"""
Per-scan trench-activity series from git history — the regime the alerts fired into.

Every committed scan snapshot carries {scan_ts, discovered, enriched, survivors:[...]}.
The counts were computed BEFORE the same run's alerts went out, so a trailing window
ending at a row's own scan is point-in-time by construction. The 2026-09 audit found
both 30x+ verticals landed in the same record pump.fun week — trench-wide risk
appetite is a regime, and this mines the series to make it a feature.

Usage:  python3 upside/mine_regime.py        # writes upside/regime.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(HERE, "regime.csv")


def _git(args: list, repo: str) -> str:
    return subprocess.run(["git", "-C", repo] + args, capture_output=True,
                          text=True, check=True).stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args()
    shas = [l for l in _git(["log", "--reverse", "--format=%H", "--",
                             "data/latest_scan.json"], args.repo).splitlines() if l]
    rows, bad = {}, 0
    for sha in shas:
        try:
            scan = json.loads(_git(["show", f"{sha}:data/latest_scan.json"], args.repo))
        except Exception:
            bad += 1
            continue
        ts = scan.get("scan_ts")
        if ts is None:
            continue
        rows[ts] = {"scan_ts": ts, "discovered": scan.get("discovered", 0),
                    "enriched": scan.get("enriched", 0),
                    "n_survivors": len(scan.get("survivors") or []),
                    "n_a": sum(1 for s in scan.get("survivors") or []
                               if s.get("tier") == "A")}
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["scan_ts", "discovered", "enriched",
                                           "n_survivors", "n_a"])
        w.writeheader()
        for ts in sorted(rows):
            w.writerow(rows[ts])
    print(f"wrote {len(rows)} scans -> {OUT}" + (f" ({bad} unparseable)" if bad else ""))


if __name__ == "__main__":
    main()
