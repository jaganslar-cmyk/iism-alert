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

# NOTE: IISM rebuilt the site as static .html pages around 12-15 Oct 2026; the
# old extensionless /course_Schedule path began returning 404 and the watcher
# went blind for ~17 days. Keep a fallback list so a single rename cannot blind
# us again, and FAIL LOUDLY (see check_open) when none of them resolve.
COURSE_URLS = [
    "https://www.iismgulmarg.in/Course_Schedule.html",
    "https://www.iismgulmarg.in/course_Schedule-2.html",
    "https://www.iismgulmarg.in/course_Schedule",
]
COURSE_URL = COURSE_URLS[0]
REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# A snow-skiing course row: name mentions ski/snowboard but NOT water.
SKI_RE = re.compile(r"ski|snowboard", re.IGNORECASE)
# Page-level proof that the WINTER schedule is published. The 2026-27 rebuild
# puts the discipline in section headings ("SNOW SKIING COURSES AT GULMARG"),
# not in a per-row course-name cell.
SNOW_HEADING_RE = re.compile(
    r"snow\s*ski|snow\s*board|snowboard", re.IGNORECASE)
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
    # No charset header on these pages: requests would default to latin-1 and
    # mangle the rupee sign. apparent_encoding sniffs the real one (utf-8).
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
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
    patterns = [
        # 2026-27 wording: "BOOKING DATE: 12 TH NOVEMBER 2026 AT 10:00 HRS"
        r"(booking\s+date\s*:?\s*\d{1,2}\s*(?:st|nd|rd|th)?\s+"
        r"[a-z]+\s+\d{4}(?:\s+at\s+\d{1,2}[:.]\d{2}\s*(?:hrs|hours|[ap]m)?)?)",
        # Older wording: "Bookings will open ... 14/11/2026 at 10:00 AM"
        r"(bookings?\s+will\s+open[^<]{0,80}?"
        r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
        r"(?:[^<]{0,25}?\d{1,2}:\d{2}\s*[AP]M)?)",
        # Generic catch-all: any "booking ... <date>" phrasing.
        r"(booking[^<]{0,40}?\d{1,2}\s*(?:st|nd|rd|th)?\s+[a-z]+\s+20\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

class ScheduleUnavailable(RuntimeError):
    """Every known schedule URL failed. This is an ERROR, never a 'not open'."""


def fetch_schedule() -> tuple[str, str]:
    """Return (url, html) for the first schedule URL that responds, else raise.

    Raising is deliberate. The previous version returned None here and the
    caller mapped that to "registration not open yet", so when IISM renamed the
    page the workflow stayed green while seeing nothing for ~17 days - straight
    through the window in which the winter schedule was actually published.
    A blind watcher must look broken, not idle.
    """
    problems = []
    for url in COURSE_URLS:
        raw = fetch(url)
        if raw is not None:
            return url, raw
        problems.append(url)
    raise ScheduleUnavailable(
        "none of the known schedule URLs could be fetched: " + ", ".join(problems))


def check_open() -> tuple[bool, str, list[dict], str | None]:
    url, raw = fetch_schedule()
    page_text = _clean(re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I))

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

    # 2026-27 layout: the discipline lives in a section heading rather than a
    # per-row cell, so a page-level snow-ski heading counts as published. Only
    # the batch rows (which carry winter dates) are reported as detail.
    heading_hit = bool(SNOW_HEADING_RE.search(page_text))
    if heading_hit and not ski_rows:
        ski_rows = [
            c for c in courses
            if WINTER_RE.search(c["row_text"])
            and not WATER_RE.search(c["row_text"])
            and not re.search(r"rafting|trek|mountaineer", c["row_text"], re.I)
        ]

    is_open = bool(ski_rows) or heading_hit

    listed = "; ".join(c["name"] for c in courses) or "no courses parsed"
    if is_open:
        found = "; ".join(c["name"] for c in ski_rows)
        evidence = (f"SNOW-SKI schedule PUBLISHED [{url}]: {found}"
                    f"  |  banner: {banner or '-'}")
    else:
        evidence = (f"no snow-ski course yet [{url}]. Currently listed: {listed}"
                    f"  |  banner: {banner or '-'}")
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

    try:
        is_open, evidence, ski_rows, banner = check_open()
    except ScheduleUnavailable as e:
        # Loud failure: red workflow + one group warning (rate-limited by state)
        # so nobody mistakes a broken watcher for a quiet one.
        print(f"[{now}] WATCHER BLIND: {e}", file=sys.stderr)
        if not dry_run and not state.get("blind_notified"):
            send_telegram(
                "\u26a0\ufe0f IISM watcher problem: the course-schedule page "
                "can't be fetched from any known URL, so the watcher is "
                "currently BLIND and cannot detect the booking opening.\n\n"
                f"Tried: {', '.join(COURSE_URLS)}\n\n"
                "Check the site for a renamed page.")
            state["blind_notified"] = now
            save_state(state)
        return 2
    # Recovered from a previous blind spell - allow a future warning again.
    if state.pop("blind_notified", None) and not dry_run:
        save_state(state)
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
