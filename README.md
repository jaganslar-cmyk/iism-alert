# IISM snow-skiing 2027 registration watcher

Polls IISM Gulmarg's course-schedule page and, the **first time a snow-skiing
course appears** (i.e. IISM publishes the winter 2026–27 schedule), posts a
one-time alert to the Telegram group "Road to 8k" via @Iism_skiing_bot — with the
course details and the "Bookings will open …" date/time banner if present.

## Why this design

- IISM registration slots fill **within minutes**, but booking opens at a
  **pre-announced date/time**. Catching the schedule *publication* gives you
  advance warning of that exact open time — which is what actually lets you win a
  seat (be ready + prepped at open).
- **Payment is decoupled:** you submit the application form (one-time mobile OTP +
  4 document uploads), then a payment LINK is emailed and paid later (~24h). The
  race is won at form submission, not payment.

## Detection

The schedule page is a static ASP.NET table. A row is flagged as snow-skiing when
its course name matches `ski`/`snowboard` but **not** `water` (excludes the summer
Water-Skiing course). Verified: correctly ignores the current summer courses and
fires on a simulated "Basic Snow Skiing" row.

## Files

- `watch.py` — checker + Telegram sender (deps: `requests`).
- `.github/workflows/watch.yml` — GitHub Actions, ~every 2h + dispatch hook.
- `state.json` — fired flag (posts once).

## Local test

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python watch.py --dry-run                 # detection only
TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… ./.venv/bin/python watch.py --test-send
```

## Deploy (GitHub Actions)

Secrets to set on the repo (Settings → Secrets and variables → Actions):
| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @Iism_skiing_bot token |
| `TELEGRAM_CHAT_ID` | `-1004484564851` |

Runs every ~2h automatically. Optional tight cadence for Oct–Dec: point a
cron-job.org job at the repo's `repository_dispatch` (type `poll`), same as the
HYROX setup.

## Timing

Last season (2025–26) booking opened **14 Nov 2025**. Expect the 2027 winter
schedule ~mid-Nov 2026 (possibly ~14 Nov 2026, 10:00 AM). Baseline 2h polling is
fine for catching the publication.
