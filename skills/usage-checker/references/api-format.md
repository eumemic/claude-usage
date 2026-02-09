# Claude Usage API Response Format

## JSON output structure (`check --json`)

The command returns a JSON array with one object per account:

```json
{
  "account_id": "1",
  "label": "eumemic@gmail.com",
  "checked_at": "2026-02-09T10:35:47.129250",
  "api_data": [ ... ],
  "error": null
}
```

If `error` is non-null, the account data is unavailable (usually expired session).

## api_data entries

Each account has two API data entries:

### Usage (`/usage` endpoint)

```json
{
  "five_hour": {
    "utilization": 7.0,
    "resets_at": "2026-02-09T21:00:00.058923+00:00"
  },
  "seven_day": {
    "utilization": 57.0,
    "resets_at": "2026-02-10T17:00:00.058953+00:00"
  },
  "seven_day_sonnet": {
    "utilization": 5.0,
    "resets_at": "2026-02-10T20:00:00.061006+00:00"
  },
  "seven_day_opus": null,
  "seven_day_cowork": null,
  "extra_usage": null
}
```

**Key fields:**
- `utilization` — percentage (0-100) of the rate limit consumed
- `resets_at` — ISO timestamp (UTC) when this window resets. `null` means no usage in this window.
- `five_hour` — rolling 5-hour session window (the "current session" limit)
- `seven_day` — rolling 7-day window across all models (the main weekly limit)
- `seven_day_sonnet` — Sonnet-specific 7-day limit (separate, more generous)
- `seven_day_opus` — Opus-specific limit (null if not applicable)
- `extra_usage` — overage/pay-per-use data (null if not enabled or unused)

### Subscription details (`/subscription_details` endpoint)

```json
{
  "next_charge_date": "2026-02-27",
  "status": "active",
  "billing_interval": "monthly",
  "payment_method": {
    "brand": "discover",
    "last4": "1058",
    "type": "card"
  }
}
```

## Pace calculation

To determine if an account is "running hot":

1. Parse `resets_at` for the 7-day usage
2. Calculate: `time_remaining = resets_at - now`
3. Calculate: `period_elapsed_pct = (7_days - time_remaining) / 7_days * 100`
4. Calculate: `pace = utilization / period_elapsed_pct`
5. Interpret: pace > 1.2 means over pace (will likely hit limit), pace < 0.8 means comfortably under

## Account mapping

| ID | Name | Email |
|----|------|-------|
| 1 | eumemic | eumemic@gmail.com |
| 2 | pelotom | pelotom@gmail.com |
| 3 | thomasmcrockett | thomasmcrockett@gmail.com |
