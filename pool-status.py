#!/usr/bin/env python3
"""Pool status report focused on gap risk, not headroom vanity metrics.

Burn rates are expressed as % of weekly capacity: 100% = exactly sustainable,
>100% = will eventually cause a blackout gap, <100% = comfortable.
"""
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(SCRIPT_DIR, "claude-usage")
HISTORY_FILE = Path(SCRIPT_DIR) / "data" / "usage-history.jsonl"

raw = subprocess.run(
    [CLI, "check", "--json"],
    capture_output=True,
    text=True,
).stdout
data = json.loads(raw)
now = datetime.now(UTC)

accounts = []
for acct in data:
    label = acct["label"].split("@")[0]
    u = acct["api_data"][0]["data"]
    s = u["seven_day"]
    reset_str = s.get("resets_at")
    reset_hrs = None
    if reset_str:
        reset = datetime.fromisoformat(reset_str)
        reset_hrs = (reset - now).total_seconds() / 3600
    accounts.append({
        "name": label,
        "used": s["utilization"],
        "remaining": 100 - s["utilization"],
        "reset_hrs": reset_hrs,
    })

num_accounts = len(accounts)
# Sustainable rate: total capacity / window length
sustainable_raw = num_accounts * 100 / 168  # %pts/hr across pool


def _load_snapshots(lookback_hours: float) -> list[dict]:
    """Load history snapshots within the lookback window."""
    if not HISTORY_FILE.exists():
        return []
    cutoff = now - timedelta(hours=lookback_hours)
    snapshots = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            ts = datetime.fromisoformat(record["ts"])
            if ts >= cutoff:
                snapshots.append(record)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return snapshots


def compute_pool_burn_rate(snapshots: list[dict]) -> float:
    """Compute pool-level burn rate from history, amortizing across resets.

    For each consecutive pair of snapshots, sum per-account deltas but clamp
    negative deltas to 0 (a reset frees capacity, it's not negative burn).
    Returns raw rate in %pts/hr across the pool.
    """
    if len(snapshots) < 2:
        return 0.0

    total_burn = 0.0
    total_hours = 0.0

    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]
        curr = snapshots[i]

        ts_prev = datetime.fromisoformat(prev["ts"])
        ts_curr = datetime.fromisoformat(curr["ts"])
        dt_hours = (ts_curr - ts_prev).total_seconds() / 3600
        if dt_hours <= 0:
            continue

        interval_burn = 0.0
        all_names = set(prev.get("accounts", {})) | set(curr.get("accounts", {}))
        for name in all_names:
            prev_acct = prev.get("accounts", {}).get(name, {})
            curr_acct = curr.get("accounts", {}).get(name, {})
            prev_util = prev_acct.get("seven_day", {}).get("utilization")
            curr_util = curr_acct.get("seven_day", {}).get("utilization")
            if prev_util is None or curr_util is None:
                continue
            delta = curr_util - prev_util
            if delta > 0:
                interval_burn += delta

        total_burn += interval_burn
        total_hours += dt_hours

    if total_hours <= 0:
        return 0.0
    return total_burn / total_hours


def to_capacity_pct(raw_rate: float) -> float:
    """Convert raw burn rate (%pts/hr) to % of weekly capacity."""
    if sustainable_raw <= 0:
        return 0.0
    return (raw_rate / sustainable_raw) * 100


# Compute burn rates at multiple lookback windows
burn_6h_raw = compute_pool_burn_rate(_load_snapshots(6))
burn_24h_raw = compute_pool_burn_rate(_load_snapshots(24))
burn_7d_raw = compute_pool_burn_rate(_load_snapshots(168))

burn_6h = to_capacity_pct(burn_6h_raw)
burn_24h = to_capacity_pct(burn_24h_raw)
burn_7d = to_capacity_pct(burn_7d_raw)

# Use 6h raw rate for gap risk (most responsive to current conditions)
total_burn = burn_6h_raw

total_remaining = sum(a["remaining"] for a in accounts)

# Sort accounts by reset time for simulation
accounts_by_reset = sorted(accounts, key=lambda a: a["reset_hrs"] or 999)
first_reset_hrs = next(
    (a["reset_hrs"] for a in accounts_by_reset if a["reset_hrs"] and a["reset_hrs"] > 0),
    None,
)

# Simulate: consume remaining capacity, then check if resets arrive in time
if total_burn > 0:
    runway_hrs = total_remaining / total_burn
else:
    runway_hrs = float("inf")

gap_hrs = 0
if total_burn > 0 and first_reset_hrs:
    burn_before_first_reset = total_burn * first_reset_hrs
    if burn_before_first_reset > total_remaining:
        gap_start = total_remaining / total_burn
        gap_hrs = first_reset_hrs - gap_start

# Output
print("Pool Status")
print("=" * 55)
print()

for a in sorted(accounts, key=lambda x: -x["used"]):
    bar_len = int(a["used"] / 5)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    if a["reset_hrs"] is not None and a["reset_hrs"] > 0:
        reset_label = f"resets in {a['reset_hrs']:.0f}h"
    elif a["used"] == 0:
        reset_label = "fresh"
    else:
        reset_label = "resetting"
    print(f"  {a['name']:<18} {bar} {a['used']:>3.0f}%  {reset_label}")

print()


def fmt_capacity(pct):
    if pct < 1:
        return "idle"
    return f"{pct:.0f}%"


print(f"  Capacity:  {fmt_capacity(burn_6h)} (6h)  "
      f"{fmt_capacity(burn_24h)} (24h)  "
      f"{fmt_capacity(burn_7d)} (7d)")
print(f"             100% = sustainable max ({num_accounts} accounts)")
print()

if total_burn == 0:
    print("  Status: idle")
elif gap_hrs > 0:
    safe_burn = total_remaining / first_reset_hrs
    safe_pct = to_capacity_pct(safe_burn)
    print(f"  ⛔ GAP PROJECTED: {gap_hrs:.0f}h blackout")
    print(f"     Runs dry in {runway_hrs:.0f}h, first reset in "
          f"{first_reset_hrs:.0f}h")
    print(f"     Need to cut to {safe_pct:.0f}% capacity to avoid gap")
else:
    # No gap projected — report status based on runway comfort
    if first_reset_hrs and runway_hrs < first_reset_hrs:
        # We'd run dry before the first reset if no resets happened,
        # but gap_hrs <= 0 means resets replenish in time
        print(f"  ⚠️  Hot — runway {runway_hrs:.0f}h but reset in "
              f"{first_reset_hrs:.0f}h saves it")
    elif first_reset_hrs:
        print(f"  ✅ Sustainable — runway {runway_hrs:.0f}h, "
              f"resets in {first_reset_hrs:.0f}h")
    else:
        print(f"  ✅ Sustainable — runway {runway_hrs:.0f}h")
