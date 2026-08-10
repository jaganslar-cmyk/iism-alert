#!/usr/bin/env python3
"""
IISM Gulmarg snow-skiing course registration watcher (2027 / winter 2026-27).

Polls the IISM course-schedule page and, the first time a SNOW-skiing course
appears in the schedule table (i.e. IISM publishes the winter courses), posts a
one-time alert to a Telegram group with the course details and — if present —
the "Bookings will open …" date/time banner. A state file prevents re-alerting.

Detection: the schedule page is a static ASP.NET table (columns: S.No, Course
Name, …, From, To, Fee). We flag a row whose course name matches ski/snowboard
but NOT "water" (to exclude the summer Water-Skiing course). This fires when the
winter snow-skiing schedule is published, giving advance notice of the exact
booking open time — which matters because slots fill within minutes.

Config via env vars:
  TELEGRAM_BOT_TOKEN  - @Iism_skiing_bot token
  TELEGRAM_CHAT_ID    - target group chat id
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"

COURSE_URL = "https://www.iismgulmarg.in/course_Schedule"
REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# A snow-skiing course row: name mentions ski/snowboard but NOT water.
SKI_RE = re.compile(r"ski|snowboard", re.IGNORECASE)
WATER_RE = re.compile(r"water", re.IGNORECASE)
# Winter timing confidence: a date in Nov/Dec or Jan–Mar, or the year 2027.
WINTER_RE = re.compile(
    r"\b\d{1,2}[./-](0?[1-3]|1[12])[./-](20)?2[67]\b|2027|"
    r"\b(dec|jan|feb|mar)[a-z]*\b",
    re.IGNORECASE,
)

APPLY_URL = "https://admission.iismgulmarg.in/Admission_instructions.aspx"


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #

def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        print(f"fetch error: {e}")
        return None
    if r.status_code != 200:
        print(f"HTTP {r.status_code} for {url}")
        return None
    return r.text


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_courses(raw: str) -> list[dict]:
    """Return a list of {name, row_text} for each course row in the schedule tables."""
    courses = []
    seen = set()
    for table in re.findall(r"<table.*?</table>", raw, re.S | re.I):
        for row in re.findall(r"<tr.*?</tr>", table, re.S | re.I):
            cells = [
                _clean(c)
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            ]
            cells = [c for c in cells if c]
            if not cells:
                continue
            row_text = " | ".join(cells)
            # Skip the header row.
            if re.search(r"course name", row_text, re.I):
                continue
            # Course name = first cell that isn't just a serial number.
            name = next((c for c in cells if not re.fullmatch(r"\d+", c)), cells[0])
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            courses.append({"name": name, "row_text": row_text})
    return courses


def find_booking_banner(raw: str) -> str | None:
    text = _clean(re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I))
    m = re.search(
        r"(bookings?\s+will\s+open[^<]{0,80}?"
        r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
        r"(?:[^<]{0,25}?\d{1,2}:\d{2}\s*[AP]M)?)",
        text,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def check_open() -> tuple[bool, str, list[dict], str | None]:
    raw = fetch(COURSE_URL)
    if raw is None:
        return False, "could not fetch schedule page", [], None

    courses = parse_courses(raw)
    ski_rows = [
        c for c in courses
        if SKI_RE.search(c["name"]) and not WATER_RE.search(c["name"])
    ]
    # Fallback: a ski/snowboard mention anywhere in a row with winter timing,
    # in case the course name column is laid out unusually.
    if not ski_rows:
        ski_rows = [
            c for c in courses
            if SKI_RE.search(c["row_text"])
            and not WATER_RE.search(c["row_text"])
            and WINTER_RE.search(c["row_text"])
        ]

    banner = find_booking_banner(raw)
    is_open = bool(ski_rows)

    listed = "; ".join(c["name"] for c in courses) or "no courses parsed"
    if is_open:
        found = "; ".join(c["name"] for c in ski_rows)
        evidence = f"SNOW-SKI course(s) listed: {found}  |  banner: {banner or '-'}"
    else:
        evidence = f"no snow-ski course yet. Currently listed: {listed}  |  banner: {banner or '-'}"
    return is_open, evidence, ski_rows, banner


def build_message(ski_rows: list[dict], banner: str | None) -> str:
    lines = ["⛷️ IISM Gulmarg snow-skiing 2026-27 registration is LIVE / published!"]
    if banner:
        lines.append(f"\n\U0001F4E2 {banner}")
    lines.append("\nCourses now listed:")
    for c in ski_rows:
        lines.append(f"• {c['row_text']}")
    lines.append(f"\nApply / details: {APPLY_URL}")
    lines.append("Schedule page: " + COURSE_URL)
    lines.append("\n⚡ Slots fill in MINUTES — get documents ready and be at the portal at open time.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"notified": False, "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(text: str) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars.")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": False},
            timeout=REQUEST_TIMEOUT,
        )
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"send error: {e}"
    ok = bool(body.get("ok"))
    detail = body["result"]["message_id"] if ok else body.get("description")
    return ok, f"HTTP {r.status_code} {detail}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    force_test = "--test-send" in sys.argv
    dry_run = "--dry-run" in sys.argv

    state = load_state()
    if state.get("notified") and not force_test:
        print(f"[{now}] Already notified — nothing to do.")
        return 0

    is_open, evidence, ski_rows, banner = check_open()
    print(f"[{now}] {evidence}")

    if not is_open and not force_test:
        if not dry_run:
            state.setdefault("history", []).append({"ts": now, "open": False})
            state["history"] = state["history"][-50:]
            save_state(state)
        print(f"[{now}] Registration not open yet.")
        return 0

    # For a forced test with no real course, show a sample message.
    if force_test and not is_open:
        ski_rows = [{"name": "Basic Snow Skiing (TEST)",
                     "row_text": "TEST | Basic Snow Skiing | 14 Days | Dec 2026 | Jan 2027"}]
    message = build_message(ski_rows, banner)

    if dry_run:
        print(f"[{now}] --dry-run; would post to Telegram:\n{message}")
        return 0

    ok, detail = send_telegram(message)
    print(f"[{now}] telegram: {'OK' if ok else 'FAIL'} ({detail})")

    if is_open:
        state["notified"] = True
        state["notified_at"] = now
    state.setdefault("history", []).append({"ts": now, "open": is_open, "sent": True})
    state["history"] = state["history"][-50:]
    save_state(state)
    print(f"[{now}] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
