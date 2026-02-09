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
    acct = accounts[account_id]
    profile_dir = PROFILES_DIR / f"account-{account_id}"
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
                lambda url: "/chat" in url or url.rstrip("/") == "https://claude.ai",
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


# ── Check: fast httpx-based usage fetch ──────────────────────────────────────

async def check_fast(account_id: str, accounts: dict) -> dict:
    """Check usage via direct HTTP calls (no browser)."""
    import httpx

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

    endpoints = []
    if ENDPOINTS_FILE.exists():
        endpoints = json.loads(ENDPOINTS_FILE.read_text())

    if not endpoints:
        result["error"] = "No endpoints discovered. Run: claude-usage discover"
        return result

    # Build cookie jar from saved cookies
    cookie_jar = httpx.Cookies()
    for c in cookies:
        cookie_jar.set(c["name"], c["value"], domain=c.get("domain", "claude.ai"))

    async with httpx.AsyncClient(
        cookies=cookie_jar,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://claude.ai/settings/usage",
        },
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        for ep in endpoints:
            url = ep["url"]
            try:
                resp = await client.get(url)

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
            except httpx.HTTPError as e:
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
        # Browser mode: sequential (can't run multiple Chromium contexts well in parallel)
        for aid in account_ids:
            print(f"Checking {accounts[aid]['label']} (browser)...", file=sys.stderr)
            r = await check_browser(aid, accounts, headless=False)
            results.append(r)
    else:
        # Fast mode: parallel httpx calls
        tasks = []
        for aid in account_ids:
            print(f"Checking {accounts[aid]['label']}...", file=sys.stderr)
            tasks.append(check_fast(aid, accounts))
        results = await asyncio.gather(*tasks)
        results = list(results)

    return results


# ── Output formatting ────────────────────────────────────────────────────────

def format_results(results: list[dict]):
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

        if r.get("page_text"):
            print(f"\n  Page content:")
            lines = [l.strip() for l in r["page_text"].splitlines() if l.strip()]
            for line in lines[:40]:
                print(f"    {line}")
            if len(lines) > 40:
                print(f"    ... ({len(lines) - 40} more lines)")

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
        else:
            format_results(results)


if __name__ == "__main__":
    main()
