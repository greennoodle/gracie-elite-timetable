"""Run the parsers against saved fixtures and print a summary."""
from pathlib import Path
from collections import Counter

from build_ics import (
    scrape_erina, scrape_peninsula, build_calendar,
)

ROOT = Path(__file__).parent
erina_html = (ROOT / "fixtures" / "erina.html").read_text()
pen_html = (ROOT / "fixtures" / "peninsula.html").read_text()

erina = scrape_erina(erina_html)
pen = scrape_peninsula(pen_html)

print(f"\nERINA: {len(erina)} classes")
print("-" * 80)
by_day = {}
for s in erina:
    by_day.setdefault(s.day_name, []).append(s)
for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
    items = by_day.get(day, [])
    print(f"  {day} ({len(items)})")
    for s in items:
        resume = f" [from {s.resume_from}]" if s.resume_from else ""
        print(f"    {s.mat:6s} {s.start_h:02d}:{s.start_m:02d} +{int(s.duration.total_seconds()/60):>3d}min "
              f"[{s.category:9s}] {s.title}{resume}")

print(f"\nPENINSULA: {len(pen)} classes")
print("-" * 80)
by_day = {}
for s in pen:
    by_day.setdefault(s.day_name, []).append(s)
for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
    items = by_day.get(day, [])
    print(f"  {day} ({len(items)})")
    for s in items:
        print(f"    {s.start_h:02d}:{s.start_m:02d} +{int(s.duration.total_seconds()/60):>3d}min "
              f"[{s.category:9s}] {s.title}")

print(f"\nErina categories: {Counter(s.category for s in erina)}")
print(f"Peninsula categories: {Counter(s.category for s in pen)}")

# Build calendars to ensure ICS generation works.
cal_e = build_calendar("Gracie Elite Erina", erina)
cal_p = build_calendar("Gracie Elite Peninsula", pen)
ics_e = cal_e.to_ical()
ics_p = cal_p.to_ical()
print(f"\nERINA ICS: {len(ics_e)} bytes, {ics_e.count(b'BEGIN:VEVENT')} VEVENTs")
print(f"PENINSULA ICS: {len(ics_p)} bytes, {ics_p.count(b'BEGIN:VEVENT')} VEVENTs")

# Show sample event
print("\nSample VEVENT (Erina, first event):")
import re
m = re.search(rb"BEGIN:VEVENT.*?END:VEVENT", ics_e, re.DOTALL)
if m:
    print(m.group(0).decode())
