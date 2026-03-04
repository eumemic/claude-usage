#!/usr/bin/env python3
"""
Check Claude Max usage across multiple Google OAuth accounts.

Hybrid approach:
  - `setup`    — Playwright browser for Google OAuth login; exports cookies to disk.
  - `discover` — Playwright loads usage page with network interception, saves API endpoints.
  - `check`    — httpx hits saved endpoints directly (fast, no browser).

Usage:
    claude-usage setup                  # Log in to all accounts
    claude-usage setup -a 1             # Log in to one account
    claude-usage discover               # Capture API endpoints from usage page
    claude-usage check                  # Fast check, all accounts
    claude-usage check -a 2             # Fast check, one account
    claude-usage check --json           # Raw JSON output
    claude-usage check --visible        # Browser fallback (if cookies expired)
    claude-usage track                  # Append snapshot to history (for cron)
    claude-usage report                 # Burn rate analysis (last 24h)
    claude-usage report --hours 6       # Shorter lookback window
    claude-usage report --json          # JSON output
    claude-usage report -a eumemic      # Filter to one account
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

SCRIPT_DIR = Path(__file__).parent
PROFILES_DIR = SCRIPT_DIR / "profiles"
CONFIG_FILE = SCRIPT_DIR / "accounts.json"
ENDPOINTS_FILE = SCRIPT_DIR / "endpoints.json"
HISTORY_FILE = SCRIPT_DIR / "data" / "usage-history.jsonl"

DEFAULT_ACCOUNTS = {
    "1": {"name": "eumemic", "label": "eumemic@gmail.com"},
    "2": {"name": "pelotom", "label": "pelotom@gmail.com"},
    "3": {"name": "thomasmcrockett", "label": "thomasmcrockett@gmail.com"},
}


def load_accounts() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    CONFIG_FILE.write_text(json.dumps(DEFAULT_ACCOUNTS, indent=2) + "\n")
    return DEFAULT_ACCOUNTS


def resolve_account(identifier: str, accounts: dict) -> str | None:
    if identifier in accounts:
        return identifier
    for key, acct in accounts.items():
        if acct["name"].lower() == identifier.lower():
            return key
    return None


# ── Cookie helpers ───────────────────────────────────────────────────────────

def cookies_path(account_id: str) -> Path:
    return PROFILES_DIR / f"account-{account_id}" / "cookies.json"


def save_cookies(account_id: str, cookies: list[dict]):
    path = cookies_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, indent=2) + "\n")


def load_cookies(account_id: str) -> list[dict] | None:
    path = cookies_path(account_id)
    if path.exists():
        return json.loads(path.read_text())
    return None


# ── Setup: interactive login + cookie export ─────────────────────────────────

async def setup_account(account_id: str, accounts: dict):
    import shutil

    acct = accounts[account_id]
    profile_dir = PROFILES_DIR / f"account-{account_id}"
    # Wipe existing profile to prevent stale sessions from auto-logging in
    # as the wrong account
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n--- Setting up: {acct['label']} ---")
    print("A browser will open. Log in with Google OAuth.")
    print("Once you see the chat page, the browser will close automatically.\n")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://claude.ai/login")

        print("Waiting for login (up to 5 minutes)...")
        try:
            await page.wait_for_url(
                lambda url: any(p in url for p in ("/chat", "/new", "/recents"))
                or url.rstrip("/") == "https://claude.ai",
                timeout=300_000,
            )
            print(f"Logged in successfully: {acct['label']}")

            # Export cookies for fast httpx-based checks
            all_cookies = await context.cookies()
            claude_cookies = [c for c in all_cookies if "claude.ai" in c.get("domain", "")]
            save_cookies(account_id, claude_cookies)
            print(f"  → Saved {len(claude_cookies)} cookies to {cookies_path(account_id)}")

        except Exception:
            print("Login timed out or browser was closed early.")

        await context.close()


async def setup_all(accounts: dict):
    for account_id in sorted(accounts.keys()):
        await setup_account(account_id, accounts)
    print("\nAll accounts set up.")
    print("Next: run 'claude-usage discover' to capture API endpoints.")


# ── Discover: capture API endpoint URLs from usage page ──────────────────────

async def discover_endpoints(account_id: str, accounts: dict):
    """Open usage page in browser, intercept API calls, save endpoint patterns."""
    acct = accounts[account_id]
    profile_dir = PROFILES_DIR / f"account-{account_id}"

    if not profile_dir.exists():
        print(f"Account {acct['label']} not set up. Run 'setup' first.", file=sys.stderr)
        return []

    print(f"Discovering endpoints for {acct['label']}...")

    captured = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        async def on_response(response):
            url = response.url
            if "claude.ai/api/" in url:
                try:
                    body = await response.json()
                    captured.append({
                        "url": url,
                        "method": response.request.method,
                        "status": response.status,
                        "sample_keys": (
                            list(body.keys()) if isinstance(body, dict)
                            else f"[array:{len(body)}]"
                        ),
                    })
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto("https://claude.ai/settings/usage", wait_until="networkidle")

            if "/login" in page.url:
                print(f"  Session expired for {acct['label']}. Re-run 'setup -a {account_id}'.")
                await context.close()
                return []

            # Wait for lazy API calls
            await page.wait_for_timeout(5000)

            # Also re-export cookies while we have a live session
            all_cookies = await context.cookies()
            claude_cookies = [c for c in all_cookies if "claude.ai" in c.get("domain", "")]
            save_cookies(account_id, claude_cookies)

        except Exception as e:
            print(f"  Error: {e}", file=sys.stderr)

        await context.close()

    return captured


async def discover_all(accounts: dict):
    """Run discovery on the first account (endpoints are the same for all)."""
    # Use the first available account for discovery
    account_id = sorted(accounts.keys())[0]
    endpoints = await discover_endpoints(account_id, accounts)

    if not endpoints:
        print("No API endpoints captured. Try running 'setup' first.", file=sys.stderr)
        return

    # Save discovered endpoints
    ENDPOINTS_FILE.write_text(json.dumps(endpoints, indent=2) + "\n")
    print(f"\nCaptured {len(endpoints)} API endpoints → {ENDPOINTS_FILE}")
    print("\nEndpoints found:")
    for ep in endpoints:
        print(f"  {ep['method']} {ep['url']}")
        print(f"    Status: {ep['status']}, Keys: {ep['sample_keys']}")

    print("\nYou can now use 'claude-usage check' for fast usage checks.")


# ── Check: fast curl_cffi-based usage fetch ──────────────────────────────────

# Endpoints to fetch per org (relative to /api/organizations/{org_id}/)
USAGE_ENDPOINTS = [
    "usage",
    "subscription_details",
]


def check_fast_sync(account_id: str, accounts: dict) -> dict:
    """Check usage via direct HTTP calls with curl_cffi (no browser)."""
    from curl_cffi import requests as curl_requests

    acct = accounts[account_id]
    result = {
        "account_id": account_id,
        "label": acct["label"],
        "checked_at": datetime.now().isoformat(),
        "api_data": [],
        "error": None,
    }

    cookies = load_cookies(account_id)
    if not cookies:
        result["error"] = f"No cookies found. Run: claude-usage setup -a {account_id}"
        return result

    cookie_dict = {c["name"]: c["value"] for c in cookies}

    def get(url):
        return curl_requests.get(url, cookies=cookie_dict, impersonate="chrome")

    # Step 1: get org UUID via bootstrap
    try:
        resp = get("https://claude.ai/api/bootstrap")
    except Exception as e:
        result["error"] = f"Network error: {e}"
        return result

    if resp.status_code in (401, 403):
        result["error"] = f"Session expired. Run: claude-usage setup -a {account_id}"
        return result

    try:
        bootstrap = resp.json()
    except Exception:
        result["error"] = f"Unexpected response: {resp.text[:200]}"
        return result

    try:
        org_id = bootstrap["account"]["memberships"][0]["organization"]["uuid"]
    except (KeyError, IndexError):
        result["error"] = f"No organization found. Run: claude-usage setup -a {account_id}"
        return result

    # Step 2: fetch usage endpoints
    for ep_path in USAGE_ENDPOINTS:
        url = f"https://claude.ai/api/organizations/{org_id}/{ep_path}"
        try:
            resp = get(url)

            if resp.status_code in (401, 403):
                result["error"] = f"Session expired. Run: claude-usage setup -a {account_id}"
                return result

            try:
                data = resp.json()
            except Exception:
                data = resp.text[:500]
            result["api_data"].append({
                "url": url,
                "status": resp.status_code,
                "data": data,
            })
        except Exception as e:
            result["api_data"].append({
                "url": url,
                "status": "error",
                "data": str(e),
            })

    return result


async def check_browser(account_id: str, accounts: dict, headless: bool = True) -> dict:
    """Fallback: check usage via full browser (slower but always works)."""
    acct = accounts[account_id]
    profile_dir = PROFILES_DIR / f"account-{account_id}"

    if not profile_dir.exists():
        return {
            "account_id": account_id,
            "label": acct["label"],
            "error": f"Not set up. Run: claude-usage setup -a {account_id}",
        }

    result = {
        "account_id": account_id,
        "label": acct["label"],
        "checked_at": datetime.now().isoformat(),
        "api_data": [],
        "page_text": None,
        "error": None,
    }

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        captured = []

        async def on_response(response):
            url = response.url
            if "claude.ai/api/" in url:
                try:
                    body = await response.json()
                    captured.append({"url": url, "status": response.status, "data": body})
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto("https://claude.ai/settings/usage", wait_until="networkidle")

            if "/login" in page.url:
                result["error"] = f"Session expired. Run: claude-usage setup -a {account_id}"
                await context.close()
                return result

            await page.wait_for_timeout(3000)

            try:
                result["page_text"] = (await page.inner_text("main")).strip()
            except Exception:
                try:
                    result["page_text"] = (await page.inner_text("body")).strip()
                except Exception:
                    pass

            result["api_data"] = captured

            # Re-export cookies while session is fresh
            all_cookies = await context.cookies()
            claude_cookies = [c for c in all_cookies if "claude.ai" in c.get("domain", "")]
            save_cookies(account_id, claude_cookies)

        except Exception as e:
            result["error"] = str(e)

        await context.close()

    return result


async def check_accounts(
    account_ids: list[str], accounts: dict, visible: bool = False,
) -> list[dict]:
    results = []

    if visible:
        # Browser mode: sequential
        for aid in account_ids:
            print(f"Checking {accounts[aid]['label']} (browser)...", file=sys.stderr)
            r = await check_browser(aid, accounts, headless=False)
            results.append(r)
    else:
        # Fast mode: curl_cffi (sync, but each call is <1s)
        for aid in account_ids:
            print(f"Checking {accounts[aid]['label']}...", file=sys.stderr)
            r = check_fast_sync(aid, accounts)
            results.append(r)

    return results


# ── Output formatting ────────────────────────────────────────────────────────

def _parse_reset_time(resets_at: str | None) -> datetime | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(resets_at)
    except Exception:
        return None


def _time_remaining(dt: datetime | None) -> str:
    if not dt:
        return "-"
    now = datetime.now(timezone.utc)
    # Make dt timezone-aware if it isn't
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - now
    total_secs = int(delta.total_seconds())
    if total_secs <= 0:
        return "now"
    hours, remainder = divmod(total_secs, 3600)
    minutes = remainder // 60
    if hours >= 24:
        days = hours // 24
        hours = hours % 24
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m"


def _pace_indicator(utilization: float, resets_at: str | None, window_hours: float) -> str:
    """Compare usage % to % of window elapsed. Returns a pace indicator."""

    if utilization == 0:
        return ""
    if not resets_at:
        return ""

    reset_dt = _parse_reset_time(resets_at)
    if not reset_dt:
        return ""

    now = datetime.now(timezone.utc)
    if reset_dt.tzinfo is None:
        reset_dt = reset_dt.replace(tzinfo=timezone.utc)

    secs_remaining = (reset_dt - now).total_seconds()
    if secs_remaining <= 0:
        return ""

    window_secs = window_hours * 3600
    secs_elapsed = window_secs - secs_remaining
    if secs_elapsed <= 0:
        return ""

    period_pct = (secs_elapsed / window_secs) * 100
    if period_pct < 1:
        return ""

    # pace = how fast you're burning relative to the clock
    # >1 means you'll hit the limit before the window resets
    pace = utilization / period_pct

    if pace >= 2.0:
        return "!!!"  # way over pace
    elif pace >= 1.2:
        return "! "   # over pace
    elif pace >= 0.8:
        return "~ "   # roughly on pace
    else:
        return "ok"   # under pace


def _billing_info(sub_data: dict) -> tuple[str, str]:
    """Return (next_billing_str, days_until_str)."""
    ncd = sub_data.get("next_charge_date")
    if not ncd:
        return ("-", "-")
    try:
        next_date = datetime.strptime(ncd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = (next_date - now).days
        return (ncd, f"{days}d")
    except Exception:
        return (ncd, "?")


def format_summary(results: list[dict]):
    """Print a compact summary table."""

    rows = []
    for r in results:
        label = r["label"].split("@")[0]  # just the username part

        if r.get("error"):
            rows.append({"label": label, "error": r["error"]})
            continue

        usage_data = None
        sub_data = None
        usage_status = None
        for entry in r.get("api_data", []):
            status = entry.get("status")
            data = entry.get("data", {})
            if not isinstance(data, dict):
                continue
            if "five_hour" in data:
                usage_data = data
            elif "next_charge_date" in data:
                sub_data = data
            elif "usage" in entry.get("url", ""):
                usage_status = status

        if not usage_data:
            if usage_status == 429:
                msg = "Usage API rate limited (429)"
            else:
                msg = "No usage data"
            rows.append({"label": label, "error": msg})
            continue

        five = usage_data.get("five_hour") or {}
        week = usage_data.get("seven_day") or {}
        sonnet = usage_data.get("seven_day_sonnet") or {}

        five_pct = five.get("utilization", 0)
        week_pct = week.get("utilization", 0)
        sonnet_pct = sonnet.get("utilization", 0)

        five_reset = _time_remaining(_parse_reset_time(five.get("resets_at")))
        week_reset = _time_remaining(_parse_reset_time(week.get("resets_at")))

        week_pace = _pace_indicator(week_pct, week.get("resets_at"), 7 * 24)
        five_pace = _pace_indicator(five_pct, five.get("resets_at"), 5)

        next_bill, days_until = ("-", "-")
        if sub_data:
            next_bill, days_until = _billing_info(sub_data)

        rows.append({
            "label": label,
            "five_pct": five_pct, "five_reset": five_reset, "five_pace": five_pace,
            "week_pct": week_pct, "week_reset": week_reset, "week_pace": week_pace,
            "sonnet_pct": sonnet_pct,
            "next_bill": next_bill, "days_until": days_until,
        })

    # Print table
    print()
    hdr = (
        f"{'Account':<20s} {'5hr':>5s} {'resets':>7s} {'7day':>5s}"
        f" {'resets':>7s} {'pace':>4s} {'sonnet':>6s} {'billing':>10s} {'in':>4s}"
    )
    print(hdr)
    print("-" * len(hdr))

    for row in rows:
        if "error" in row:
            print(f"{row['label']:<20s} ERROR: {row['error']}")
            continue

        print(
            f"{row['label']:<20s}"
            f" {row['five_pct']:>4.0f}%"
            f" {row['five_reset']:>7s}"
            f" {row['week_pct']:>4.0f}%"
            f" {row['week_reset']:>7s}"
            f" {row['week_pace']:>4s}"
            f" {row['sonnet_pct']:>5.0f}%"
            f" {row['next_bill']:>10s}"
            f" {row['days_until']:>4s}"
        )

    print()
    print("Pace: ok = under pace, ~ = on pace, ! = over pace, !!! = way over")
    print()


def format_detail(results: list[dict]):
    """Print detailed API response data."""
    divider = "=" * 60

    for r in results:
        print(f"\n{divider}")
        print(f"  {r['label']}")
        print(divider)

        if r.get("error"):
            print(f"  ERROR: {r['error']}")
            continue

        if r.get("api_data"):
            for entry in r["api_data"]:
                url = entry["url"]
                url_path = url.split("claude.ai")[-1] if "claude.ai" in url else url
                print(f"\n  API: {url_path}")
                data = entry["data"]
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            print(f"    {k}:")
                            for k2, v2 in v.items():
                                print(f"      {k2}: {v2}")
                        elif isinstance(v, list) and len(v) > 3:
                            print(f"    {k}: [{len(v)} items]")
                        else:
                            print(f"    {k}: {v}")
                elif isinstance(data, list):
                    for item in data[:5]:
                        print(f"    - {json.dumps(item, default=str)}")
                    if len(data) > 5:
                        print(f"    ... ({len(data) - 5} more)")
                else:
                    print(f"    {data}")

    print()


# ── Track: append usage snapshot to JSONL history ────────────────────────────

def _extract_usage_snapshot(check_result: dict) -> dict:
    """Transform a check_fast_sync result into compact usage fields."""
    if check_result.get("error"):
        return {"error": check_result["error"]}

    usage_data = None
    for entry in check_result.get("api_data", []):
        data = entry.get("data", {})
        if isinstance(data, dict) and "five_hour" in data:
            usage_data = data
            break

    if not usage_data:
        return {"error": "no usage data"}

    snapshot = {}
    for window in ("five_hour", "seven_day", "seven_day_sonnet"):
        w = usage_data.get(window)
        if w:
            snapshot[window] = {
                "utilization": w.get("utilization", 0),
                "resets_at": w.get("resets_at"),
            }
    return snapshot


def track_usage(accounts: dict) -> None:
    """Fetch all accounts and append one JSONL snapshot line."""
    account_snapshots = {}
    for aid in sorted(accounts.keys()):
        name = accounts[aid]["name"]
        print(f"Checking {name}...", file=sys.stderr)
        result = check_fast_sync(aid, accounts)
        account_snapshots[name] = _extract_usage_snapshot(result)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "accounts": account_snapshots,
    }

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# ── Report: burn rate analysis from history ──────────────────────────────────

def load_history(hours: float = 24.0) -> list[dict]:
    """Read JSONL history, filter to lookback window."""
    if not HISTORY_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    snapshots = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            ts = datetime.fromisoformat(record["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            snapshots.append(record)
    return snapshots


def _find_monotonic_segment(
    points: list[tuple[float, float]], drop_threshold: float = 5.0,
) -> list[tuple[float, float]]:
    """Find most recent segment without large drops.

    Walking backwards, if utilization drops by more than drop_threshold
    between consecutive points (old usage falling off rolling window),
    truncate to after the drop.
    """
    if len(points) < 2:
        return points
    # points are sorted by time ascending
    for i in range(len(points) - 1, 0, -1):
        if points[i][1] - points[i - 1][1] < -drop_threshold:
            return points[i:]
    return points


def _linear_regression_slope(
    points: list[tuple[float, float]],
) -> float | None:
    """Least-squares slope in units_y per units_x. Returns None if <2 points."""
    n = len(points)
    if n < 2:
        return None
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_x2 = sum(p[0] ** 2 for p in points)
    denom = n * sum_x2 - sum_x**2
    if abs(denom) < 1e-10:
        return None
    return (n * sum_xy - sum_x * sum_y) / denom


def compute_burn_rate(
    snapshots: list[dict], account_name: str, window_key: str,
) -> dict | None:
    """Compute burn rate for one account+window from history snapshots."""
    # Extract (hours_since_first, utilization) pairs
    points_raw = []
    for snap in snapshots:
        acct = snap.get("accounts", {}).get(account_name, {})
        if "error" in acct:
            continue
        window = acct.get(window_key, {})
        util = window.get("utilization")
        if util is None:
            continue
        ts = datetime.fromisoformat(snap["ts"])
        points_raw.append((ts, util))

    if not points_raw:
        return None

    # Sort by time
    points_raw.sort(key=lambda p: p[0])

    # Convert to hours since first snapshot
    t0 = points_raw[0][0]
    points = [
        ((t - t0).total_seconds() / 3600, u)
        for t, u in points_raw
    ]

    # Find monotonic segment (handles drops from rolling window)
    segment = _find_monotonic_segment(points)
    slope = _linear_regression_slope(segment)

    current_util = points_raw[-1][1]
    resets_at_str = None
    # Get resets_at from latest snapshot
    latest_acct = snapshots[-1].get("accounts", {}).get(
        account_name, {},
    )
    latest_window = latest_acct.get(window_key, {})
    resets_at_str = latest_window.get("resets_at")

    result = {
        "account": account_name,
        "window": window_key,
        "current": current_util,
        "slope_per_hour": slope,
        "data_points": len(segment),
        "total_points": len(points_raw),
        "resets_at": resets_at_str,
    }

    if slope and slope > 0.01:
        eta_hours = (100 - current_util) / slope
        result["eta_hours"] = round(eta_hours, 1)

        # Compare to reset time
        if resets_at_str:
            reset_dt = _parse_reset_time(resets_at_str)
            if reset_dt:
                now = datetime.now(timezone.utc)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(
                        tzinfo=timezone.utc,
                    )
                hours_to_reset = (
                    (reset_dt - now).total_seconds() / 3600
                )
                result["hours_to_reset"] = round(
                    hours_to_reset, 1,
                )
                result["will_hit_limit"] = (
                    eta_hours < hours_to_reset
                )

    return result


def assess_pool_health(report_data: list[dict]) -> dict:
    """Assess token pool as a whole, accounting for rotation.

    Returns pool-level health with:
    - available: accounts under 100% weekly
    - weekly_runway: hours until last available account hits 100%
    - session_available: accounts under 100% on 5hr window
    - alert level: ok / warning / critical
    """
    weekly = [r for r in report_data if r["window"] == "seven_day"]
    session = [r for r in report_data if r["window"] == "five_hour"]
    total = len(weekly)

    # Weekly pool health
    weekly_available = [r for r in weekly if r["current"] < 100]
    weekly_capped = total - len(weekly_available)

    # Pool headroom: total remaining capacity across uncapped accounts
    pool_headroom = sum(100 - r["current"] for r in weekly_available)

    # Active burn rate: max among uncapped accounts (only one burns
    # at a time due to sequential rotation)
    active_burn = max(
        (r.get("slope_per_hour", 0) for r in weekly_available
         if r.get("slope_per_hour") and r["slope_per_hour"] > 0.01),
        default=0,
    )

    # Earliest reset across all accounts (capped ones will free up)
    earliest_reset_hours = None
    for r in weekly:
        h = r.get("hours_to_reset")
        if h is not None and h > 0:
            if earliest_reset_hours is None or h < earliest_reset_hours:
                earliest_reset_hours = h

    # Session pool health
    session_available = [
        r for r in session if r["current"] < 100
    ]

    # Pool runway: headroom / active_burn (sequential rotation)
    pool_runway = (
        pool_headroom / active_burn if active_burn > 0.01 else None
    )
    # If a capped account resets before the pool is exhausted,
    # capacity is replenished — runway is effectively infinite
    if (earliest_reset_hours and pool_runway
            and earliest_reset_hours < pool_runway):
        pool_runway = None

    # Determine alert level
    last_eta = pool_headroom / active_burn if active_burn > 0.01 else None
    if not weekly_available:
        alert = "critical"
        alert_reason = "all accounts at weekly cap"
    elif len(weekly_available) == 1:
        if last_eta is not None and last_eta < 2:
            alert = "critical"
            alert_reason = (
                f"last account ({weekly_available[0]['account']})"
                f" hits cap in ~{last_eta:.0f}h"
            )
        else:
            alert = "warning"
            alert_reason = (
                f"only {weekly_available[0]['account']} has"
                f" weekly headroom"
            )
    elif len(weekly_available) == 2 and all(
        r["current"] > 80 for r in weekly_available
    ):
        alert = "warning"
        alert_reason = (
            "only 2 accounts left, both above 80%"
        )
    elif not session_available:
        alert = "warning"
        alert_reason = (
            "all session caps hit (resolves in <5h)"
        )
    else:
        alert = "ok"
        alert_reason = (
            f"{len(weekly_available)}/{total} accounts"
            f" have weekly headroom"
        )

    return {
        "total_accounts": total,
        "weekly_available": len(weekly_available),
        "weekly_capped": weekly_capped,
        "session_available": len(session_available),
        "session_total": len(session),
        "active_burn_rate": round(active_burn, 1),
        "pool_headroom_pct": round(pool_headroom, 1),
        "pool_runway_hours": (
            round(pool_runway, 1) if pool_runway else None
        ),
        "earliest_reset_hours": (
            round(earliest_reset_hours, 1)
            if earliest_reset_hours else None
        ),
        "alert": alert,
        "alert_reason": alert_reason,
    }


def recommend_account(report_data: list[dict]) -> dict | None:
    """Pick best account by utilization + burn rate."""
    candidates = []
    for entry in report_data:
        if entry.get("window") != "seven_day":
            continue
        score = entry["current"]
        # Penalize high burn rate
        if entry.get("slope_per_hour") and entry["slope_per_hour"] > 0:
            score += entry["slope_per_hour"] * 10
        candidates.append((score, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    best = candidates[0][1]
    return {
        "account": best["account"],
        "utilization": best["current"],
        "reason": _recommendation_reason(best),
    }


def _recommendation_reason(entry: dict) -> str:
    util = entry["current"]
    slope = entry.get("slope_per_hour")
    if util < 10:
        return "barely used"
    elif util < 40:
        if slope and slope < 0.5:
            return "low usage, comfortable pace"
        return "low usage"
    elif util < 70:
        return "moderate usage"
    else:
        return "high usage but lowest available"


def format_report(
    report_data: list[dict],
    pool_health: dict,
    recommendation: dict | None,
    snapshots: list[dict],
    hours: float,
    json_output: bool = False,
) -> None:
    """Print burn rate report in human or JSON format."""
    if json_output:
        output = {
            "snapshots": len(snapshots),
            "hours": hours,
            "accounts": report_data,
            "pool": pool_health,
            "recommendation": recommendation,
        }
        print(json.dumps(output, indent=2, default=str))
        return

    print(
        f"\nUsage Report"
        f" (last {hours:.0f}h, {len(snapshots)} snapshots)"
    )
    print()

    # Group by account, show seven_day as primary
    weekly = [r for r in report_data if r["window"] == "seven_day"]
    if not weekly:
        print("No data available.")
        return

    hdr = (
        f"{'Account':<20s} {'7day':>5s} {'burn':>8s}"
        f" {'ETA':>7s} {'resets':>8s} {'status':>16s}"
    )
    print(hdr)
    print("\u2500" * len(hdr))

    for r in weekly:
        name = r["account"]
        current = f"{r['current']:.0f}%"
        slope = r.get("slope_per_hour")
        burn = (
            f"+{slope:.1f}/hr" if slope and slope > 0.01
            else "\u2500"
        )
        eta = (
            f"{r['eta_hours']:.1f}h" if "eta_hours" in r
            else "\u2500"
        )
        resets = _time_remaining(
            _parse_reset_time(r.get("resets_at")),
        )

        if r["current"] >= 100:
            status = "capped"
        elif r.get("will_hit_limit"):
            status = "\u26a0 will hit limit"
        else:
            status = "ok"

        print(
            f"{name:<20s} {current:>5s} {burn:>8s}"
            f" {eta:>7s} {resets:>8s} {status:>16s}"
        )

    # Pool health summary
    ph = pool_health
    alert = ph["alert"]
    icons = {"ok": "\u2713", "warning": "\u26a0", "critical": "\u2716"}
    icon = icons.get(alert, "?")

    print(f"\nPool: {icon} {ph['alert_reason']}")

    avail = ph["weekly_available"]
    total = ph["total_accounts"]
    capped = ph["weekly_capped"]
    print(
        f"  Weekly: {avail}/{total} available,"
        f" {capped} capped"
    )

    if ph["pool_headroom_pct"] > 0:
        n_avail = ph["weekly_available"]
        print(
            f"  Pool headroom: {ph['pool_headroom_pct']:.0f}%"
            f" across {n_avail} account{'s' if n_avail != 1 else ''}"
        )

    if ph["active_burn_rate"] > 0:
        print(
            f"  Active burn: +{ph['active_burn_rate']}/hr"
            f" (sequential rotation)"
        )

    if ph["pool_runway_hours"] is not None:
        runway = ph["pool_runway_hours"]
        print(f"  Runway: ~{runway:.0f}h until all accounts capped")

    if ph["earliest_reset_hours"] is not None:
        reset_h = ph["earliest_reset_hours"]
        print(f"  Next reset: {reset_h:.0f}h (replenishes pool)")

    sess = ph["session_available"]
    sess_total = ph["session_total"]
    if sess < sess_total:
        print(
            f"  Sessions: {sess}/{sess_total}"
            f" available (5hr caps)"
        )

    if recommendation:
        print(
            f"\nRecommendation: Use"
            f" {recommendation['account']}"
            f" ({recommendation['utilization']:.0f}% weekly,"
            f" {recommendation['reason']})"
        )

    print()


def report_usage(
    accounts: dict,
    hours: float = 24.0,
    json_output: bool = False,
    account_filter: str | None = None,
) -> None:
    """Load history and print burn rate report."""
    snapshots = load_history(hours)
    if not snapshots:
        print(
            "No history data. Run 'claude-usage track' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine which accounts to report on
    if account_filter:
        account_names = [account_filter]
    else:
        # Gather all account names from snapshots
        names: set[str] = set()
        for snap in snapshots:
            names.update(snap.get("accounts", {}).keys())
        account_names = sorted(names)

    report_data = []
    for name in account_names:
        for window in ("seven_day", "five_hour"):
            result = compute_burn_rate(snapshots, name, window)
            if result:
                report_data.append(result)

    pool = assess_pool_health(report_data)
    rec = recommend_account(report_data)
    format_report(
        report_data, pool, rec, snapshots, hours, json_output,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Check Claude Max usage across accounts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  setup      Log in via browser, export session cookies
  discover   Capture API endpoints from usage page
  check      Fast usage check (httpx), or --visible for browser fallback
  track      Append usage snapshot to JSONL history (for cron)
  report     Burn rate analysis from history
""",
    )
    sub = parser.add_subparsers(dest="command")

    # setup
    sp_setup = sub.add_parser("setup", help="Log in to accounts interactively")
    sp_setup.add_argument("--account", "-a", help="Account number or name (default: all)")

    # discover
    sub.add_parser("discover", help="Capture API endpoint URLs from the usage page")

    # track
    sub.add_parser("track", help="Append usage snapshot to history (for cron)")

    # report
    sp_report = sub.add_parser("report", help="Burn rate analysis from history")
    sp_report.add_argument(
        "--hours", type=float, default=24.0, help="Lookback window (default: 24)",
    )
    sp_report.add_argument("--json", action="store_true", help="JSON output")
    sp_report.add_argument("--account", "-a", help="Filter to one account name")

    # check
    sp_check = sub.add_parser("check", help="Check usage for accounts")
    sp_check.add_argument("--account", "-a", help="Account number or name (default: all)")
    sp_check.add_argument(
        "--visible", action="store_true", help="Use browser instead of fast HTTP mode",
    )
    sp_check.add_argument("--json", action="store_true", help="Output raw JSON")
    sp_check.add_argument("--detail", "-d", action="store_true", help="Show detailed API responses")

    args = parser.parse_args()
    accounts = load_accounts()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "setup":
        if args.account:
            aid = resolve_account(args.account, accounts)
            if not aid:
                print(f"Unknown account: {args.account}", file=sys.stderr)
                sys.exit(1)
            asyncio.run(setup_account(aid, accounts))
        else:
            asyncio.run(setup_all(accounts))

    elif args.command == "discover":
        asyncio.run(discover_all(accounts))

    elif args.command == "track":
        track_usage(accounts)

    elif args.command == "report":
        report_usage(
            accounts,
            hours=args.hours,
            json_output=args.json,
            account_filter=args.account,
        )

    elif args.command == "check":
        if args.account:
            aid = resolve_account(args.account, accounts)
            if not aid:
                print(f"Unknown account: {args.account}", file=sys.stderr)
                sys.exit(1)
            target_ids = [aid]
        else:
            target_ids = sorted(accounts.keys())

        results = asyncio.run(check_accounts(target_ids, accounts, visible=args.visible))

        if args.json:
            print(json.dumps(results, indent=2, default=str))
        elif args.detail:
            format_detail(results)
        else:
            format_summary(results)


if __name__ == "__main__":
    main()
