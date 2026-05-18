# Gracie Elite Timetable Feeds

Scrapes the Gracie Elite Central Coast and Gracie Elite The Peninsula class
timetables and publishes them as ICS feeds you can subscribe to in Google
Calendar (or anywhere else).

## What it produces

After GitHub Pages is enabled, two URLs:

- `https://<your-user>.github.io/<repo>/erina.ics`
- `https://<your-user>.github.io/<repo>/peninsula.ics`

Each is a weekly recurring calendar of every class on the source page,
keyed in `Australia/Sydney`, with the location prefixed to each event title
(eg "Erina - Fundamentals (Gi)" or "Peninsula - All Levels GI").

Each event carries a `CATEGORIES` tag, one of: Adults, Kids, MuayThai,
Wrestling, Womens, OpenMat, Yoga. You can filter in Google Calendar by
searching the category name.

## Setup

1. Create a new GitHub repo and push these files.
2. In repo Settings &rarr; Pages, set Source to "Deploy from branch", pick
   `gh-pages` &rarr; `/`. (The branch is created on the first successful
   workflow run.)
3. Push to `main`. The Actions workflow will run, build both feeds, and
   push to `gh-pages`.
4. After the first run, grab the two URLs above and subscribe in Google
   Calendar: "Other calendars" &rarr; "+" &rarr; "From URL".

## How it stays fresh

A scheduled Action runs every 6 hours, re-scrapes both sites, and rewrites
`gh-pages`. Google Calendar polls subscribed feeds roughly every 8 to 24
hours. Worst case end-to-end staleness is about a day.

UIDs are deterministic hashes of `(location, day, start time, title, mat)`.
That means:

- Time or title changes update the existing event in place.
- Classes that disappear from the source page disappear from the feed,
  which Google Calendar removes on the next refresh.

## Local testing

```bash
pip install -r requirements.txt
python build_ics.py              # writes public/erina.ics and public/peninsula.ics
python test_parse.py             # parser smoke test against saved fixtures
```

## Known fragilities

The two source pages use static HTML rendered by Elementor and may change
layout in future. The parsers are defensive about typos and spacing but
will break if either site moves to a JavaScript widget. If that happens,
the fix is to inspect the network tab and hit the underlying API directly
rather than re-render the page.
