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
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

SCRIPT_DIR = Path(__file__).parent
PROFILES_DIR = SCRIPT_DIR / "profiles"
CONFIG_FILE = SCRIPT_DIR / "accounts.json"
ENDPOINTS_FILE = SCRIPT_DIR / "endpoints.json"

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
                        "sample_keys": list(body.keys()) if isinstance(body, dict) else f"[array:{len(body)}]",
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

            if resp.status_code == 200:
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


async def check_accounts(account_ids: list[str], accounts: dict, visible: bool = False) -> list[dict]:
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
    from datetime import timezone
    # Handle ISO format with timezone
    s = resets_at.replace("+00:00", "+0000").replace("Z", "+0000")
    try:
        # Python 3.11+ handles this natively
        return datetime.fromisoformat(resets_at)
    except Exception:
        return None


def _time_remaining(dt: datetime | None) -> str:
    if not dt:
        return "-"
    from datetime import timezone
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
    from datetime import timezone

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
    from datetime import timezone
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
    from datetime import timezone

    rows = []
    for r in results:
        label = r["label"].split("@")[0]  # just the username part

        if r.get("error"):
            rows.append({"label": label, "error": r["error"]})
            continue

        usage_data = None
        sub_data = None
        for entry in r.get("api_data", []):
            data = entry.get("data", {})
            if not isinstance(data, dict):
                continue
            if "five_hour" in data:
                usage_data = data
            elif "next_charge_date" in data:
                sub_data = data

        if not usage_data:
            rows.append({"label": label, "error": "No usage data"})
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
    hdr = f"{'Account':<20s} {'5hr':>5s} {'resets':>7s} {'7day':>5s} {'resets':>7s} {'pace':>4s} {'sonnet':>6s} {'billing':>10s} {'in':>4s}"
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
""",
    )
    sub = parser.add_subparsers(dest="command")

    # setup
    sp_setup = sub.add_parser("setup", help="Log in to accounts interactively")
    sp_setup.add_argument("--account", "-a", help="Account number or name (default: all)")

    # discover
    sub.add_parser("discover", help="Capture API endpoint URLs from the usage page")

    # check
    sp_check = sub.add_parser("check", help="Check usage for accounts")
    sp_check.add_argument("--account", "-a", help="Account number or name (default: all)")
    sp_check.add_argument("--visible", action="store_true", help="Use browser instead of fast HTTP mode")
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
