# Harvard Events Map

An interactive map of upcoming Harvard alumni-club events worldwide, pinned by
location. A single self-contained static page (`public/index.html`) plus a
Python scraper that refreshes the event data.

This is a standalone project — it was extracted from a larger multi-app repo so
it can live, deploy, and version on its own.

## How it works

- **`public/index.html`** — the whole app: an interactive [Leaflet](https://leafletjs.com/)
  map (free OpenStreetMap tiles, no API key) that plots events from `events.json`.
  It's zero-build: open it in a browser or host the `public/` folder anywhere static.
- **`public/events.json`** — a real scraped snapshot of upcoming events. The page
  loads this on every visit, so the deployed site always shows genuine, geocoded
  data. Regenerate it with the scraper below.
- **`scraper/`** — a pure-standard-library Python scraper (no dependencies, no API
  keys). Most Harvard clubs run on the central `clubs.harvard.edu` platform, whose
  `/events.html` pages embed a Google Calendar "add event" link per event (title,
  exact start/end, timezone, geocodable address) — clean structured data we parse
  directly. A few self-hosted clubs emit schema.org Event JSON-LD, handled by a
  generic parser. Geocoding uses the free OpenStreetMap Nominatim service, cached
  aggressively on disk to respect its rate limits.

## Refreshing the event data

```bash
cd scraper
py harvard_events_scraper.py     # quick manual test: prints what it found
py harvard_snapshot.py --refresh # scrape fresh + write ../public/events.json
```

Run this from a residential IP — many club sites block datacenter IPs. Every
source is fetched concurrently and wrapped in try/except, so one slow, blocked,
or changed site never breaks the others. Results and geocodes are cached to
gitignored dotfiles in `scraper/` so repeat runs are fast.

## Deploying (Cloudflare, push-to-deploy)

The site is the static contents of `public/`, deployed as a Cloudflare
static-assets Worker (see `wrangler.jsonc` → `assets.directory: "./public"`).
This repo is connected to Cloudflare via Git integration, so **every push to
`main` auto-deploys** — Cloudflare runs `npx wrangler deploy`, which uploads
`public/` as static assets. There is no build step.

To refresh the event data and republish:

```bash
py scraper/harvard_snapshot.py --refresh   # rewrites public/events.json
git commit -am "refresh events snapshot" && git push
```

If you ever want to deploy manually from a machine with Node installed:

```bash
npx wrangler login       # one-time browser auth
npx wrangler deploy      # uploads ./public per wrangler.jsonc
```

Any static host works too (GitHub Pages, Netlify, etc.) — just serve `public/`.
