#!/usr/bin/env python3
"""
IISM admission-form canary.

Two jobs, both aimed at 12 Nov 2026 10:00 IST:

1. STRUCTURE. The day-of autofill (~/iism/fill_ego.sh) addresses the form by
   element id. If IISM renames or drops a field, the fill silently misses it.
   This checks every id the autofill depends on and shouts if any disappears,
   so it gets fixed BEFORE booking day rather than during it.

2. GO-LIVE. Reads the live #ddlCourse options. The moment a snow-ski course is
   listed, booking is open - alert hard. This is a better signal than the
   schedule page: it watches the thing we actually have to fill.

Exits non-zero on any problem so the workflow goes red (a quiet failure is how
this watcher went blind for 17 days in Oct 2026).
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
STATE_FILE = HERE / "canary_state.json"
MOBILE = os.environ.get("IISM_MOBILE", "8618870632")
FORM_URL = f"https://admission.iismgulmarg.in/admission_form.aspx?mobile={MOBILE}"
TIMEOUT = 30
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Every element the day-of autofill touches. Losing any of these breaks it.
REQUIRED_IDS = [
    "ddlCourse", "ddlCourseLevel", "ddlScheduleCourse", "ddlState",
    "ddlEduationQualification", "ddlVegetarianOrNon",
    "txtName", "txtFatherName", "txtNationality", "txtDateOfBirth",
    "txtHouseNoApartment", "txtStreetVillageColony", "txtDistrictCity",
    "txtPincode", "txtMobileNo", "txtEmailId", "txtBloodGroup",
    "txtEmergencyContactName", "txtEmergencyContactPhone",
    "txtHeight", "txtWeight", "txtWaist", "txtShoeSize",
    "FupPhototOfApplicant", "FupSignatureOfAppicant",
    "FupEducationQualification", "FupBirthCertificate", "chkIAgree",
]


# Fields that exist but the autofill deliberately leaves blank (all optional,
# and verified not applicable: parent/guardian = under-18, skiing certificate =
# Intermediate/Advance, domicile + employment proof not required).
KNOWN_OPTIONAL_IDS = [
    "txtDomicile", "txtMedicalCategory", "txtAvailableSeat", "txtCourseFee",
    "FupDomicileAttachProof", "FupAnyAdventureCoursePhoto",
    "FupCurrentEmployment", "FupParentGuardianSignature",
]


def form_control_ids(raw: str) -> list[str]:
    """Every input/select id on the page, so we can spot NEW fields."""
    ids = []
    for m in re.finditer(r'<(input|select)\b[^>]*>', raw, re.I):
        tag = m.group(0)
        if re.search(r'type="(hidden|submit|button|image|reset)"', tag, re.I):
            continue
        i = re.search(r'\bid="([^"]+)"', tag)
        if i:
            ids.append(i.group(1))
    return sorted(set(ids))


def is_snow(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip().lower()
    if "water" in t:
        return False
    return bool(re.search(r"snow|ski|board", t))


def fetch(url: str) -> str:
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def course_options(raw: str) -> list[str]:
    m = re.search(r'<select[^>]*id="ddlCourse".*?</select>', raw, re.S | re.I)
    if not m:
        return []
    return [
        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", o))).strip()
        for o in re.findall(r"<option[^>]*>(.*?)</option>", m.group(0), re.S | re.I)
    ]


def send_telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("no telegram creds; skipping send")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=TIMEOUT)
        return bool(r.json().get("ok"))
    except Exception as e:
        print(f"telegram error: {e}")
        return False


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dry = "--dry-run" in sys.argv
    state = load_state()
    problems = []

    try:
        raw = fetch(FORM_URL)
    except Exception as e:
        msg = (f"⚠️ IISM CANARY: cannot load the admission form at all.\n\n{e}\n\n"
               "The day-of autofill depends on this page. Check manually.")
        print(f"[{now}] FORM UNREACHABLE: {e}", file=sys.stderr)
        if not dry and not state.get("unreachable_alerted"):
            send_telegram(msg)
            state["unreachable_alerted"] = now
            STATE_FILE.write_text(json.dumps(state, indent=2))
        return 2
    state.pop("unreachable_alerted", None)

    # --- 1. structure ---
    missing = [i for i in REQUIRED_IDS if f'id="{i}"' not in raw]
    if missing:
        problems.append(f"{len(missing)} form field(s) MISSING: {', '.join(missing)}")

    # --- 1b. NEW fields ---
    # A field we do not know about is the one risk a read-back verification
    # cannot see: the form would verify "clean" while a new required box sits
    # empty. Surface it so the autofill can be taught about it before 12 Nov.
    known = set(REQUIRED_IDS) | set(KNOWN_OPTIONAL_IDS)
    present = form_control_ids(raw)
    unknown = [i for i in present if i not in known]
    if unknown:
        problems.append(f"{len(unknown)} NEW form field(s) appeared: {', '.join(unknown)}")

    # --- 2. go-live ---
    opts = course_options(raw)
    real = [o for o in opts if o and not o.lower().startswith("select")]
    snow = [o for o in real if is_snow(o)]

    print(f"[{now}] fields ok={len(REQUIRED_IDS)-len(missing)}/{len(REQUIRED_IDS)}  "
          f"unknown={len(unknown)}  courses={real}  snow={snow}")

    if snow and not state.get("golive_alerted"):
        msg = ("⛷️⚠️ IISM BOOKING IS LIVE RIGHT NOW — the snow-ski course "
               "is in the admission dropdown.\n\n"
               f"Listed: {', '.join(snow)}\n\n"
               "GO. Run:  sh ~/iism/fill_ego.sh\n"
               "(or --pounce if it is not already running)")
        print(f"[{now}] GO-LIVE: {snow}")
        if not dry:
            send_telegram(msg)
            state["golive_alerted"] = now
            state["golive_courses"] = snow
            STATE_FILE.write_text(json.dumps(state, indent=2))

    # Course list drift (not necessarily snow) - worth a look.
    prev = state.get("courses")
    if prev is not None and prev != real and not snow:
        msg = ("❗ IISM course dropdown changed.\n\n"
               f"Was: {prev}\nNow: {real}\n\n"
               "If the snow-ski course is there under an unexpected name, check manually.")
        print(f"[{now}] COURSE LIST CHANGED {prev} -> {real}")
        if not dry:
            send_telegram(msg)
    if not dry:
        state["courses"] = real
        STATE_FILE.write_text(json.dumps(state, indent=2))

    if problems:
        text = ("⚠️ IISM CANARY FAILED — the admission form changed shape.\n\n"
                + "\n".join(problems)
                + "\n\nThe day-of autofill addresses fields by id and will MISS these. "
                  "Fix ~/iism/fill.mjs before 12 Nov.")
        print(f"[{now}] PROBLEMS: {problems}", file=sys.stderr)
        if not dry and not state.get("structure_alerted"):
            send_telegram(text)
            state["structure_alerted"] = now
            STATE_FILE.write_text(json.dumps(state, indent=2))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
