---
name: usage-checker
description: >
  This skill should be used when the user asks "check my usage", "how's my Claude usage",
  "am I running low", "which account has capacity", "check usage", "how much usage do I have left",
  "usage report", "which account should I use", mentions Claude Max rate limits, or asks about
  any of their Claude Max accounts.
---

# Claude Max Usage Checker

Check usage across all configured Claude Max accounts and report results in plain language.

## Checking Usage

Run this command to get structured JSON data for all accounts:

```bash
~/code/claude-usage/claude-usage check --json 2>/dev/null
```

Parse the JSON output and present a human-readable summary. Do not show raw JSON to the user. The number of accounts is determined by the output — do not assume a fixed count.

To check a single account by number or name:

```bash
~/code/claude-usage/claude-usage check --json -a pelotom 2>/dev/null
```

## Interpreting Results

For each account, extract these fields from the JSON `api_data`:

- **5-hour session usage** — `five_hour.utilization` (0-100%). The short-term rate limit.
- **7-day weekly usage** — `seven_day.utilization` (0-100%). The main weekly limit. This is the most important number.
- **Sonnet-only usage** — `seven_day_sonnet.utilization`. Separate limit just for Sonnet.
- **Reset times** — `resets_at` fields. Convert to human-relative times ("resets in 4h", "resets tomorrow").
- **Subscription status** — from `subscription_details` data: `next_charge_date`, `status`.

### Pace Assessment

Compare the percentage of weekly usage consumed against the percentage of the 7-day window that has elapsed:

- `time_remaining = resets_at - now`
- `window_elapsed = (7 days - time_remaining) / 7 days`
- `pace = utilization / (window_elapsed * 100)`

Describe pace in plain language:
- pace < 0.5 → "plenty of room"
- pace 0.5–0.8 → "comfortable"
- pace 0.8–1.0 → "on pace to use the full limit"
- pace 1.0–1.5 → "running a bit hot"
- pace > 1.5 → "burning through usage fast"

If `resets_at` is null, no usage has been recorded — report as "completely fresh."

### Choosing an Account

When the user asks which account to use, recommend the one with the lowest 7-day utilization. Factor in reset times — an account at 60% that resets in 2 hours is better than one at 30% that resets in 6 days.

## Handling Errors

If a result has `"error"` set (non-null), the session has expired. Instruct the user:

```
Run: ~/code/claude-usage/claude-usage setup -a {account_number}
```

This opens a browser for Google OAuth re-login. The user must run this in their own terminal — it cannot be run from within Claude Code.

## Account Management

Accounts are configured in `~/code/claude-usage/accounts.json`. To see which accounts exist:

```bash
cat ~/code/claude-usage/accounts.json
```

### First-time setup or session refresh

```bash
~/code/claude-usage/claude-usage setup          # all accounts
~/code/claude-usage/claude-usage setup -a 2     # just account #2
```

Setup opens a Playwright browser for each account. The user logs in via Google OAuth. Cookies are exported to `~/code/claude-usage/profiles/account-{N}/cookies.json`. Sessions typically last weeks before expiring.

## Example Response Style

When the user asks "how's my usage?", respond conversationally:

> **pelotom** is at 57% of the weekly limit with 22 hours until reset — comfortable pace. **thomasmcrockett** has barely been touched (3% weekly). **eumemic** is completely fresh. I'd use eumemic or thomasmcrockett next.

Lead with the most important information (which accounts are getting close to limits). Only mention billing dates if the user asks.

## Additional Resources

- **`references/api-format.md`** — Full JSON response schema, field definitions, and pace calculation details
