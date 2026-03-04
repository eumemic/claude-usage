---
name: usage-checker
description: >
  This skill should be used when the user asks "check my usage", "how's my Claude usage",
  "am I running low", "which account has capacity", "check usage", "how much usage do I have left",
  "usage report", "burn rate", "how fast am I burning", "which account should I use",
  mentions Claude Max rate limits, or asks about any of their Claude Max accounts.
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

## Burn Rate Analysis

For trend analysis over time, use the `report` command:

```bash
~/code/claude-usage/claude-usage report 2>/dev/null
~/code/claude-usage/claude-usage report --json 2>/dev/null
~/code/claude-usage/claude-usage report --hours 6 -a eumemic 2>/dev/null
```

This reads from JSONL history (`~/code/claude-usage/data/usage-history.jsonl`) populated by the `track` command. It computes burn rates via linear regression and projects ETAs to limit.

If the user asks about burn rate and there's no history data, tell them to set up tracking:

```
# Record snapshots (run via cron every 15 minutes):
~/code/claude-usage/claude-usage track

# Cron entry:
# */15 * * * * ~/code/claude-usage/claude-usage track 2>>~/code/claude-usage/data/track-errors.log
```

When presenting report data, focus on:
- Which accounts are burning fastest (highest slope_per_hour)
- Which will hit the limit before reset (will_hit_limit flag)
- The recommendation for which account to use next

## Pool Health Analysis

The `report` command includes pool-level analysis that accounts for token rotation.

### Key Concepts

- **Pool headroom**: Total remaining capacity across all uncapped accounts. E.g., if 4 accounts are at 20%, 50%, 80%, 100%, headroom = 80+50+20 = 150%.
- **Active burn rate**: The rate of the currently-burning account (highest slope among uncapped). Only one account burns at a time due to rotation.
- **Pool runway**: headroom / active_burn_rate. How long until ALL accounts are capped if burning continues at the current rate with sequential rotation.
- **Safe if reset comes first**: If any capped account resets before the pool runway expires, capacity is replenished.

### Alert Levels

- **ok**: Multiple accounts have headroom, runway is comfortable
- **warning**: Down to 1-2 accounts, or both remaining are above 80%
- **critical**: All accounts capped, or last account hits cap in <2h

### Example Interpretation

> Pool headroom: 150% across 3 accounts
> Active burn: +2.5/hr (sequential rotation)
> Runway: ~60h until all accounts capped
> Next reset: 18h

This means: at 2.5%/hr, 150% of headroom lasts 60 hours. The first account resets in 18 hours, well before exhaustion. The pool is healthy.

## Additional Resources

- **`references/api-format.md`** — Full JSON response schema, field definitions, and pace calculation details
