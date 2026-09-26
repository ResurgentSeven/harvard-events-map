#!/usr/bin/env python3
"""Write a real scraped snapshot of Harvard club events into the published site.

The site (public/index.html) is a static page hosted on Cloudflare Pages, so it
can't scrape club sites on demand. This script runs the scrape *here* (from a
residential IP, with a warm geocode cache) and writes the result to

    public/events.json

which ships with the static site. The page loads that snapshot on every visit,
so visitors see real, geocoded, worldwide event data -- refreshed whenever you
run this script and redeploy.

Usage:
    py harvard_snapshot.py            # use the cached scrape if it's fresh
    py harvard_snapshot.py --refresh  # force a fresh scrape first

Then redeploy:  npx wrangler pages deploy public --project-name harvard-events-map
"""
import json
import sys
from pathlib import Path

import harvard_events_scraper as scraper

# scraper/ -> repo root -> public/events.json
OUT = Path(__file__).resolve().parent.parent / "public" / "events.json"


def main():
    refresh = "--refresh" in sys.argv or "-r" in sys.argv
    data = scraper.get_events(refresh=refresh)
    data["ok"] = True
    data["snapshot"] = True  # let the page distinguish a published snapshot from a live fetch
    OUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for s in data["sources"] if s["ok"])
    pinned = sum(1 for e in data["events"] if e.get("lat") is not None)
    print(f"Wrote {OUT}: {len(data['events'])} events "
          f"({pinned} mapped) from {ok}/{len(data['sources'])} club sites.")
    print("Redeploy:  npx wrangler pages deploy public --project-name harvard-events-map")


if __name__ == "__main__":
    main()
