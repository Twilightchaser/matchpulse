"""Measure the simulator's distributions and print the insight thresholds they imply.

    python scripts/calibrate.py            # 6 matches, prints percentiles and suggested constants

Thresholds in app/calibration.py are percentiles of these distributions rather than raw counts,
so they keep working when the simulator's event density changes. Re-run after simulator changes.
"""

from __future__ import annotations

import statistics as st
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.calibration import high_regains, turnover_rate  # noqa: E402
from app.metrics import MOMENTUM_WINDOW_SECONDS, momentum_shares  # noqa: E402
from app.simulator import SyntheticMatch, TEAMS  # noqa: E402

SEEDS = (15, 16, 30, 41, 44, 46)


def measure(seed: int) -> dict[str, list[float]]:
    events = sorted(SyntheticMatch(seed=seed).generate(), key=lambda e: e.sequence)
    home, away = TEAMS[0].team_id, TEAMS[1].team_id
    out = {"leader_share": [], "turnover_rate": [], "high_regains": [], "events_per_window": []}
    for minute in range(6, 91):
        cutoff = minute * 60
        window = [e for e in events if cutoff - MOMENTUM_WINDOW_SECONDS < e.match_second <= cutoff]
        shares = momentum_shares(window, home, away)
        out["leader_share"].append(max(shares.values()))
        out["turnover_rate"].append(turnover_rate(window))
        out["events_per_window"].append(len(window))
        for team in (home, away):
            out["high_regains"].append(len(high_regains(window, team)))
    return out


def pct(values: list[float], q: int) -> float:
    return st.quantiles(values, n=100)[q - 1]


def main() -> None:
    merged: dict[str, list[float]] = {}
    with ProcessPoolExecutor(max_workers=2) as pool:
        for result in pool.map(measure, SEEDS):
            for key, values in result.items():
                merged.setdefault(key, []).extend(values)
    for key, values in merged.items():
        print(f"{key:18} p10 {pct(values, 10):6.3f}  p25 {pct(values, 25):6.3f}  p50 {pct(values, 50):6.3f}  "
              f"p75 {pct(values, 75):6.3f}  p90 {pct(values, 90):6.3f}  p95 {pct(values, 95):6.3f}")
    print("\nSuggested app/calibration.py values:")
    print(f"CONTROLLED_TURNOVER_RATE = {pct(merged['turnover_rate'], 20):.3f}   # p20")
    print(f"CHAOTIC_TURNOVER_RATE = {pct(merged['turnover_rate'], 80):.3f}      # p80")
    print(f"CONTROL_SHARE = {pct(merged['leader_share'], 90):.1f}               # p90 leader share")
    print(f"SHIFT_SHARE = {pct(merged['leader_share'], 60):.1f}                 # p60 leader share")
    print(f"DECISIVE_SHARE = {pct(merged['leader_share'], 50):.1f}              # p50 leader share")
    print(f"HIGH_PRESS_REGAINS = {max(2, round(pct(merged['high_regains'], 93)))}   # p93 per team window")


if __name__ == "__main__":
    main()
