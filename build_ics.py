"""
Scrape Gracie Elite timetables (Erina and Peninsula) and emit two ICS feeds.

Output files (written to OUTPUT_DIR, default ./public):
  - erina.ics
  - peninsula.ics

Design notes:
- Each location has its own parser because the HTML markup and the time
  formats differ. Common bits (day map, event building, ICS writing) are
  shared.
- Events recur weekly forever (RRULE=FREQ=WEEKLY). To retire a class, just
  re-run the scraper after it disappears from the source page; the UID will
  vanish from the feed and Google Calendar will drop it on next refresh.
- UIDs are deterministic hashes of (location, day, start time, title) so
  Google Calendar updates events in place rather than duplicating.
- All times are Australia/Sydney. icalendar emits VTIMEZONE automatically.
- "Resuming <date>" notes (Erina has one for Piranhas BJJ) push DTSTART
  forward to that date.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from curl_cffi import requests
from bs4 import BeautifulSoup, Tag
from icalendar import Calendar, Event

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("gracie")

TZ = ZoneInfo("Australia/Sydney")
DEFAULT_DURATION = timedelta(minutes=60)

DAYS = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}

# Matches things like: "10am", "10.00 am", "9.30 am", "5,00 pm" (typo on site),
# "11.30am", "6:00 pm"
TIME_RX = re.compile(
    r"(\d{1,2})\s*[.:,]?\s*(\d{2})?\s*(am|pm)",
    re.IGNORECASE,
)

# "Resuming 2nd Feb 2026" / "Resuming 2 February 2026"
RESUMING_RX = re.compile(
    r"Resuming\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


@dataclass
class ClassSlot:
    """One recurring weekly class."""
    location: str           # "Erina" or "Peninsula"
    location_address: str   # full street address for VEVENT LOCATION
    day_name: str           # "Monday" etc
    weekday: int            # 0=Mon ... 6=Sun
    start_h: int            # 24h
    start_m: int
    duration: timedelta
    title: str              # raw class name, eg "Fundamentals (Gi)"
    category: str           # Adults, Kids, Womens, MuayThai, Wrestling, OpenMat, Yoga
    mat: str = ""           # "Mat-1" / "Mat-2" / "" if single mat
    resume_from: date | None = None  # if class starts later than today

    def stable_uid(self) -> str:
        key = f"{self.location}|{self.day_name}|{self.start_h:02d}{self.start_m:02d}|{self.title}|{self.mat}"
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return f"{h}@gracie-timetable"


def parse_time(text: str) -> tuple[int, int] | None:
    """Return (hour24, minute) or None if no time found."""
    m = TIME_RX.search(text)
    if not m:
        return None
    h = int(m.group(1))
    mins = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3).lower()
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mins <= 59):
        return None
    return h, mins


def parse_resuming(text: str) -> date | None:
    m = RESUMING_RX.search(text)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = MONTHS.get(month_name)
    if not month:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def classify(title: str) -> str:
    """Bucket a class title into a coarse category."""
    t = title.lower()
    if "muay thai" in t or "kick box" in t:
        return "MuayThai"
    if "wrestl" in t:
        return "Wrestling"
    if "yoga" in t:
        return "Yoga"
    if "women" in t:
        return "Womens"
    if any(
        k in t
        for k in ("piranhas", "baby sharks", "little sharks", "kids", "child")
    ):
        return "Kids"
    if "open mat" in t:
        return "OpenMat"
    return "Adults"


# ---------------------------------------------------------------------------
# Erina (graciecentralcoast.com.au)
# ---------------------------------------------------------------------------

ERINA_URL = "https://graciecentralcoast.com.au/classes-timetable/"
ERINA_ADDRESS = "224 Penrose Crescent, Erina NSW 2250"

# Matches lines like: "Mat-1 10am All Levels (GI)" or "Mat-2 5.45 pm Women's Only..."
ERINA_LINE_RX = re.compile(
    r"^\s*(Mat[-\s]?\d+)?\s*(.+)$",
    re.IGNORECASE,
)


def scrape_erina(html: str) -> list[ClassSlot]:
    soup = BeautifulSoup(html, "html.parser")
    # Strategy: get the timetable section text, split on day names.
    # The page renders each class as a paragraph; pulling all text and then
    # splitting on day markers is far more robust than trying to navigate
    # Elementor's deeply nested divs.
    text = soup.get_text("\n", strip=True)

    # Constrain to the timetable section: from "Timetables for new classes"
    # heading up to the first footer marker.
    start_marker = "Timetables for new classes"
    end_markers = ("CALL US AND GET", "Classes Gracie Brazilian")
    s_idx = text.find(start_marker)
    if s_idx == -1:
        log.warning("Erina: timetable start marker not found")
        return []
    section = text[s_idx:]
    for em in end_markers:
        e_idx = section.find(em)
        if e_idx != -1:
            section = section[:e_idx]
            break

    # Split into per-day blocks.
    day_names = list(DAYS.keys())
    # Build a regex that matches a day name on its own line.
    day_split_rx = re.compile(
        r"^(" + "|".join(day_names) + r")\s*$", re.MULTILINE
    )
    parts = day_split_rx.split(section)
    # parts[0] is preamble; then pairs of (day_name, block).
    slots: list[ClassSlot] = []
    for i in range(1, len(parts), 2):
        day = parts[i]
        block = parts[i + 1] if i + 1 < len(parts) else ""
        slots.extend(_parse_erina_day(day, block))
    return slots


def _parse_erina_day(day: str, block: str) -> list[ClassSlot]:
    """
    BeautifulSoup's get_text("\n") puts each <strong>Mat-N</strong> on its
    own line, separate from the rest of the class description. So we need
    to merge a bare "Mat-N" line into the line that follows it. We also
    merge "Resuming ..." trailers into the line above them.
    """
    weekday = DAYS[day]
    raw_lines = [ln.strip() for ln in block.splitlines() if ln.strip()]

    # Step 1: stitch standalone "Mat-N" / "Mat N" lines onto the next line.
    mat_only_rx = re.compile(r"^Mat[-\s]?\d+$", re.IGNORECASE)
    stitched: list[str] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if mat_only_rx.match(line) and i + 1 < len(raw_lines):
            stitched.append(f"{line} {raw_lines[i + 1]}")
            i += 2
        else:
            stitched.append(line)
            i += 1

    # Step 2: collapse "Resuming ..." continuation lines into the prior line.
    lines: list[str] = []
    for ln in stitched:
        if ln.lower().startswith("resuming") and lines:
            lines[-1] = lines[-1] + " | " + ln
        else:
            lines.append(ln)

    out: list[ClassSlot] = []
    for ln in lines:
        # Skip stray section labels like "ADVANCED".
        if ln.upper() == "ADVANCED":
            continue
        slot = _parse_erina_line(day, weekday, ln)
        if slot:
            out.append(slot)
    return out


def _parse_erina_line(day: str, weekday: int, line: str) -> ClassSlot | None:
    # Extract mat marker if present.
    mat = ""
    m = re.match(r"(Mat[-\s]?\d+)\s+(.+)$", line, re.IGNORECASE)
    rest = line
    if m:
        mat = m.group(1).replace(" ", "-").title().replace("mat", "Mat")
        # Normalise: "Mat-1" / "Mat-2"
        mat = re.sub(r"Mat[-\s]?(\d+)", r"Mat-\1", mat, flags=re.IGNORECASE)
        rest = m.group(2)

    time_match = TIME_RX.search(rest)
    if not time_match:
        return None
    parsed = parse_time(rest)
    if not parsed:
        return None
    h, mm = parsed

    # Title is everything after the time match.
    title = rest[time_match.end():].strip(" -|")
    # Strip any "Resuming ..." trailer for the title; capture the date.
    resume_from = parse_resuming(title)
    title = RESUMING_RX.sub("", title).strip(" |-")
    if not title:
        return None

    return ClassSlot(
        location="Erina",
        location_address=ERINA_ADDRESS,
        day_name=day,
        weekday=weekday,
        start_h=h,
        start_m=mm,
        duration=DEFAULT_DURATION,
        title=title,
        category=classify(title),
        mat=mat,
        resume_from=resume_from,
    )


# ---------------------------------------------------------------------------
# Peninsula (gracieelitethepeninsula.com.au)
# ---------------------------------------------------------------------------

PENINSULA_URL = "https://gracieelitethepeninsula.com.au/timetable/"
PENINSULA_ADDRESS = "13 The Boulevarde, Woy Woy NSW 2257"

# Matches: "6.00 am - 7.00 am All Levels GI" or "10.00 am 11.15 am No GI..."
PEN_LINE_RX = re.compile(
    r"^\s*(\d{1,2}[.:]?\d{0,2}\s*(?:am|pm))\s*[-–]?\s*(\d{1,2}[.:]?\d{0,2}\s*(?:am|pm))\s+(.+)$",
    re.IGNORECASE,
)


def scrape_peninsula(html: str) -> list[ClassSlot]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    start_marker = "Find Your Perfect Training Session"
    end_marker = "Special Workshops"
    s_idx = text.find(start_marker)
    if s_idx == -1:
        log.warning("Peninsula: start marker not found")
        return []
    section = text[s_idx:]
    e_idx = section.find(end_marker)
    if e_idx != -1:
        section = section[:e_idx]

    day_names = list(DAYS.keys())
    day_split_rx = re.compile(
        r"^(" + "|".join(day_names) + r")\s*$", re.MULTILINE
    )
    parts = day_split_rx.split(section)
    slots: list[ClassSlot] = []
    for i in range(1, len(parts), 2):
        day = parts[i]
        block = parts[i + 1] if i + 1 < len(parts) else ""
        slots.extend(_parse_peninsula_day(day, block))
    return slots


def _parse_peninsula_day(day: str, block: str) -> list[ClassSlot]:
    weekday = DAYS[day]
    out: list[ClassSlot] = []
    for ln in (l.strip() for l in block.splitlines() if l.strip()):
        slot = _parse_peninsula_line(day, weekday, ln)
        if slot:
            out.append(slot)
    return out


def _parse_peninsula_line(day: str, weekday: int, line: str) -> ClassSlot | None:
    # Find start time and end time. End time may be missing a separator
    # (saw "10.00 am 11.15 am No GI All Levels" on Saturday).
    # Find ALL time matches in the line, take the first two as start and end.
    matches = list(TIME_RX.finditer(line))
    if len(matches) < 2:
        return None

    start = parse_time(matches[0].group(0))
    end = parse_time(matches[1].group(0))
    if not start or not end:
        return None

    sh, sm = start
    eh, em = end
    start_dt = datetime(2000, 1, 1, sh, sm)
    end_dt = datetime(2000, 1, 1, eh, em)
    if end_dt <= start_dt:
        # Implausible duration; fall back to default.
        duration = DEFAULT_DURATION
    else:
        duration = end_dt - start_dt

    # Title is everything after the second time match.
    title = line[matches[1].end():].strip(" -–|")
    if not title:
        return None

    return ClassSlot(
        location="Peninsula",
        location_address=PENINSULA_ADDRESS,
        day_name=day,
        weekday=weekday,
        start_h=sh,
        start_m=sm,
        duration=duration,
        title=title,
        category=classify(title),
        mat="",
        resume_from=None,
    )


# ---------------------------------------------------------------------------
# ICS building
# ---------------------------------------------------------------------------

def next_weekday_on_or_after(start: date, weekday: int) -> date:
    """Return the next date >= start that falls on the given weekday (0=Mon)."""
    delta = (weekday - start.weekday()) % 7
    return start + timedelta(days=delta)


def build_calendar(name: str, slots: list[ClassSlot]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//gracie-timetable//andrew//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "Australia/Sydney")
    cal.add("method", "PUBLISH")

    today = datetime.now(TZ).date()

    for slot in slots:
        ev = Event()
        ev.add("uid", slot.stable_uid())
        ev.add("summary", f"{slot.location} - {slot.title}")

        location_parts = [slot.location_address]
        if slot.mat:
            location_parts.insert(0, slot.mat)
        ev.add("location", ", ".join(location_parts))

        desc_lines = [
            f"Category: {slot.category}",
            f"Day: {slot.day_name}",
        ]
        if slot.mat:
            desc_lines.append(f"Mat: {slot.mat}")
        desc_lines.append(f"Class: {slot.title}")
        ev.add("description", "\n".join(desc_lines))

        ev.add("categories", [slot.category])

        # Pick a first-occurrence date.
        base_start = slot.resume_from if slot.resume_from else today
        first_date = next_weekday_on_or_after(base_start, slot.weekday)
        dtstart = datetime(
            first_date.year, first_date.month, first_date.day,
            slot.start_h, slot.start_m, tzinfo=TZ,
        )
        dtend = dtstart + slot.duration
        ev.add("dtstart", dtstart)
        ev.add("dtend", dtend)
        ev.add("dtstamp", datetime.now(TZ))
        ev.add("rrule", {"freq": "weekly"})

        cal.add_component(ev)

    return cal


def write_ics(cal: Calendar, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cal.to_ical())
    log.info("Wrote %s (%d bytes)", path, path.stat().st_size)


def fetch(url: str) -> str:
    """
    Fetch a page using curl_cffi to impersonate a real Chrome browser.

    The plain `requests` library trips Cloudflare-style WAF fingerprinting
    on these sites (returns 403). curl_cffi replays Chrome's actual TLS
    handshake and header ordering, which the WAFs trust.

    Tries a few impersonation profiles in case the site fingerprints
    against a specific version.
    """
    profiles = ["chrome124", "chrome120", "safari17_0", "firefox133"]
    last_err: Exception | None = None
    for profile in profiles:
        log.info("Fetching %s (impersonate=%s)", url, profile)
        try:
            r = requests.get(url, timeout=30, impersonate=profile)
            if r.status_code == 200:
                return r.text
            log.warning("Got HTTP %s with profile %s", r.status_code, profile)
        except Exception as exc:
            log.warning("Profile %s failed: %s", profile, exc)
            last_err = exc
    raise RuntimeError(
        f"All impersonation profiles failed for {url}. Last error: {last_err}"
    )


def main() -> int:
    out_dir = Path(os.environ.get("OUTPUT_DIR", "public"))

    erina_html = fetch(ERINA_URL)
    erina_slots = scrape_erina(erina_html)
    log.info("Erina: parsed %d classes", len(erina_slots))

    peninsula_html = fetch(PENINSULA_URL)
    peninsula_slots = scrape_peninsula(peninsula_html)
    log.info("Peninsula: parsed %d classes", len(peninsula_slots))

    if not erina_slots or not peninsula_slots:
        log.error("One of the timetables produced zero classes; aborting.")
        return 1

    write_ics(
        build_calendar("Gracie Elite Erina", erina_slots),
        out_dir / "erina.ics",
    )
    write_ics(
        build_calendar("Gracie Elite Peninsula", peninsula_slots),
        out_dir / "peninsula.ics",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
