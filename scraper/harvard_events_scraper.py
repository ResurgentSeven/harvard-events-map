#!/usr/bin/env python3
"""Local-only Harvard club event scraper for the harvard-events-map app.

This lives on the *local* server only: server.py lazy-imports it to answer
GET /api/harvard-events. A browser can't scrape club sites directly (CORS), and
many of them block datacenter IPs anyway, but the Python server on your PC can --
same trick as the /sync button. The public Cloudflare build is static and has no
server, so the app there falls back to its bundled sample of events.

Pure standard library, so the zero-dependency promise still holds. No API keys
or tokens are required: geocoding uses the free OpenStreetMap Nominatim service
(cached aggressively to respect its 1 req/sec policy), and the map tiles in the
app are free OSM tiles.

How it actually scrapes
-----------------------
Most Harvard alumni clubs worldwide run their public site on one central platform
at ``<slug>.clubs.harvard.edu``. Their ``/events.html`` page renders every
upcoming event server-side as an ``<li>`` inside ``<ul class="med-image-list">``,
and -- crucially -- embeds a Google Calendar "add event" link per event:

    calendar.google.com/calendar/r/eventedit?text=<title>&dates=<start>/<end>
        &ctz=<tz>&location=<full address>

That link is clean, machine-readable structured data (title, exact start/end,
timezone, geocodable address), so we parse it directly -- far more robust than
guessing HTML. Events without a calendar link fall back to the visible
``<h4>`` title + the ``"5:00PM - 6:30PM Thu 25 Jun 2026"`` date line, and any
event still missing a location is pinned to its club's home city so it always
lands on the map.

A few clubs self-host (WordPress etc.) and instead emit schema.org "Event"
JSON-LD; those are listed in JSONLD_SOURCES and handled by a generic JSON-LD
parser. Adding a club is a one-line edit to either list.

Robustness: every source is fetched concurrently and wrapped in try/except, so
one slow/blocked/changed site never breaks the others. The whole result is
cached to .harvard_events_cache.json with a TTL so page loads are cheap.
"""
import json
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / ".harvard_events_cache.json"
GEOCODE_CACHE_FILE = ROOT / ".harvard_geocode_cache.json"
CACHE_TTL_SECONDS = 6 * 60 * 60  # club calendars change slowly; refetch a few times a day
FETCH_TIMEOUT = 12               # per-site; a blocked/slow club shouldn't stall the batch
MAX_WORKERS = 24                 # fetch clubs concurrently so ~100 sites take seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Nominatim asks every client to identify itself; this is just a courtesy UA, not a key.
NOMINATIM_UA = "WebApps-HarvardEventsMap/1.0 (personal hobby project)"

# --- Clubs on the central clubs.harvard.edu platform -------------------------- #
# (display name, subdomain, home city used to geocode events with no address).
# The home city guarantees every event lands somewhere on the map even when the
# venue is unlisted. This is the full Harvard Clubs directory of clubs running on
# the clubs.harvard.edu platform (alumni.harvard.edu/community/clubs-sigs/clubs-directory).
# Clubs that don't resolve or have no upcoming events simply report 0 and are
# harmless; add or remove a line to taste.
CLUBS = [
    # ---- United States ----
    ("Harvard Club of Boston", "hcboston", "Boston, MA, USA"),
    ("Harvard Club of Cape Cod", "capecod", "Hyannis, MA, USA"),
    ("Harvard Club of New Bedford-Fall River", "hcnewbedfordfallriver", "New Bedford, MA, USA"),
    ("Harvard Club of New York City", "hcnewyork", "New York, NY, USA"),
    ("Harvard Club of Long Island", "hcli", "Garden City, NY, USA"),
    ("Harvard Club of the Hudson Valley", "hchudsonvalley", "Poughkeepsie, NY, USA"),
    ("Harvard University Club of Eastern New York", "hceasternnewyork", "Albany, NY, USA"),
    ("Harvard-Radcliffe Club of Rochester", "hrcrochester", "Rochester, NY, USA"),
    ("Harvard-Radcliffe Club of Westchester", "hrcwestchester", "White Plains, NY, USA"),
    ("Harvard Club of Sacramento", "hcsacramento", "Sacramento, CA, USA"),
    ("Harvard Club of San Diego", "hcsandiego", "San Diego, CA, USA"),
    ("Harvard Club of San Francisco", "hcsanfrancisco", "San Francisco, CA, USA"),
    ("Harvard Club of Southern California", "hcsc", "Los Angeles, CA, USA"),
    ("Harvard Club of Chicago", "hcchicago", "Chicago, IL, USA"),
    ("First Coast Harvard Club", "hcfirstcoast", "Jacksonville, FL, USA"),
    ("Harvard Club of Miami", "hcmiami", "Miami, FL, USA"),
    ("Harvard Club of Tampa Bay", "hctampabay", "Tampa, FL, USA"),
    ("Harvard Club of the Palm Beaches", "hcpalmbeaches", "West Palm Beach, FL, USA"),
    ("Harvard Club of Broward County", "hcbrowardcounty", "Fort Lauderdale, FL, USA"),
    ("Harvard-Radcliffe Club of Northern Connecticut", "hrcnorthernconnecticut", "Hartford, CT, USA"),
    ("Harvard Club of Alabama", "hcbirmingham", "Birmingham, AL, USA"),
    ("Harvard Club of the Mid-Gulf Coast", "hcmidgulfcoast", "Mobile, AL, USA"),
    ("Harvard Club of Arkansas", "hcarkansas", "Little Rock, AR, USA"),
    ("Harvard Club of Phoenix", "hcphoenix", "Phoenix, AZ, USA"),
    ("Harvard Club of Delaware", "hcdelaware", "Wilmington, DE, USA"),
    ("Harvard Club of Washington, DC", "hcdc", "Washington, DC, USA"),
    ("Harvard Club of Georgia", "hcgeorgia", "Atlanta, GA, USA"),
    ("Harvard Club of Savannah and Coastal Georgia", "hcsavannah", "Savannah, GA, USA"),
    ("Harvard Alumni Contact of Maui", "hcmaui", "Kahului, HI, USA"),
    ("Harvard Club of Hawaii", "hchawaii", "Honolulu, HI, USA"),
    ("Harvard Club of Indiana", "hcindiana", "Indianapolis, IN, USA"),
    ("Harvard Club of Iowa", "hciowa", "Des Moines, IA, USA"),
    ("Harvard Club of Louisville", "hclouisville", "Louisville, KY, USA"),
    ("Harvard Club of Louisiana", "hclouisiana", "New Orleans, LA, USA"),
    ("Harvard Club in Maine", "hcmaine", "Portland, ME, USA"),
    ("Harvard Club of Maryland", "hcmaryland", "Baltimore, MD, USA"),
    ("Harvard Alumni Contact of Central Michigan", "hccentralmichigan", "Lansing, MI, USA"),
    ("Harvard Club of Detroit", "hceasternmichigan", "Detroit, MI, USA"),
    ("Harvard Club of St. Louis", "hcstlouis", "St. Louis, MO, USA"),
    ("Harvard University Club of Houston", "hchouston", "Houston, TX, USA"),
    ("Harvard Club of Austin", "hcaustin", "Austin, TX, USA"),
    ("Harvard Club of San Antonio", "hcsanantonio", "San Antonio, TX, USA"),
    ("Harvard Club of Dallas", "hcdallas", "Dallas, TX, USA"),
    ("Harvard Club of Nebraska", "hcnebraska", "Omaha, NE, USA"),
    ("Harvard Club of New Hampshire", "hcnewhampshire", "Manchester, NH, USA"),
    ("Harvard Club of New Jersey", "hcnj", "Newark, NJ, USA"),
    ("Harvard-Radcliffe Club of New Mexico", "hcnewmexico", "Albuquerque, NM, USA"),
    ("Harvard Club of Western North Carolina", "hcwnc", "Asheville, NC, USA"),
    ("Harvard Club of the Research Triangle", "hcresearchtriangle", "Raleigh, NC, USA"),
    ("Harvard Club of Northeast Ohio", "hcnortheastohio", "Cleveland, OH, USA"),
    ("Harvard Club of Oklahoma City", "hcoklahomacity", "Oklahoma City, OK, USA"),
    ("Harvard Club of Oregon", "hcoregon", "Portland, OR, USA"),
    ("Harvard Club of Philadelphia", "hrcphilly", "Philadelphia, PA, USA"),
    ("Harvard Club of Western Pennsylvania", "hcwesternpennsylvania", "Pittsburgh, PA, USA"),
    ("Harvard Club of Puerto Rico", "hcpuertorico", "San Juan, Puerto Rico"),
    ("Harvard Club of Rhode Island", "hcrhodeisland", "Providence, RI, USA"),
    ("Harvard Club of South Carolina", "hcsouthcarolina", "Columbia, SC, USA"),
    ("Harvard Club of Middle Tennessee", "hcmiddletennessee", "Nashville, TN, USA"),
    ("Harvard Alumni Association of Utah", "harvardutah", "Salt Lake City, UT, USA"),
    ("Harvard-Radcliffe Club of Vermont", "hrcvermont", "Burlington, VT, USA"),
    ("Harvard Club of West Virginia", "hcwestvirginia", "Charleston, WV, USA"),
    ("Harvard Club of Wisconsin", "hcwisconsin", "Milwaukee, WI, USA"),
    ("Harvard Club of Seattle", "hcseattle", "Seattle, WA, USA"),
    # ---- Canada ----
    ("Harvard Club of Atlantic Canada", "hcatlanticcanada", "Halifax, NS, Canada"),
    ("Harvard Club of Toronto", "hctoronto", "Toronto, Ontario, Canada"),
    # ---- Africa ----
    ("Harvard Club of Egypt", "hcegypt", "Cairo, Egypt"),
    ("Harvard Club of Kenya", "hckenya", "Nairobi, Kenya"),
    ("Harvard University Alumni of South Africa", "huasouthafrica", "Johannesburg, South Africa"),
    # ---- Asia-Pacific ----
    ("Harvard Club of Australia", "hcaustralia", "Sydney, Australia"),
    ("Harvard Club of Beijing", "hcbeijing", "Beijing, China"),
    ("Harvard Club of Indonesia", "hcindonesia", "Jakarta, Indonesia"),
    ("Harvard Club of Japan", "hcjapan", "Tokyo, Japan"),
    ("Harvard Club of Korea", "hckorea", "Seoul, South Korea"),
    ("Harvard Club of Pakistan", "hcpakistan", "Karachi, Pakistan"),
    ("Harvard Club of Vietnam", "hcvietnam", "Ho Chi Minh City, Vietnam"),
    # ---- Europe ----
    ("Harvard Club of Armenia", "hcarmenia", "Yerevan, Armenia"),
    ("Harvard Club of Belgium", "hcbelgium", "Brussels, Belgium"),
    ("Harvard Club of Bulgaria", "hcbulgaria", "Sofia, Bulgaria"),
    ("Harvard Club of Croatia", "hccroatia", "Zagreb, Croatia"),
    ("Harvard Club of Cyprus", "hccyprus", "Nicosia, Cyprus"),
    ("Harvard Club of Denmark", "hcdenmark", "Copenhagen, Denmark"),
    ("Harvard Club of Georgia (Europe)", "hcrepublicgeorgia", "Tbilisi, Georgia"),
    ("Harvard Club of Ireland", "hcireland", "Dublin, Ireland"),
    ("Harvard Club of Italy", "hcitaly", "Rome, Italy"),
    ("Harvard Club of Luxembourg", "hcluxembourg", "Luxembourg City, Luxembourg"),
    ("Harvard Club of Portugal", "hcportugal", "Lisbon, Portugal"),
    ("Harvard Club of Romania and Moldova", "hcromaniaandmoldova", "Bucharest, Romania"),
    ("Harvard Club of Russia", "hcrussia", "Moscow, Russia"),
    ("Harvard Club of Spain", "hcspain", "Madrid, Spain"),
    ("Harvard Club of Switzerland", "hcswitzerland", "Zurich, Switzerland"),
    ("Harvard Club of the United Kingdom", "hcuk", "London, United Kingdom"),
    # ---- Latin America & Caribbean ----
    ("Harvard Club de Argentina", "hcargentina", "Buenos Aires, Argentina"),
    ("Harvard Club of Bolivia", "hcbolivia", "La Paz, Bolivia"),
    ("Harvard Alumni Club of Brazil", "hacbrazil", "Sao Paulo, Brazil"),
    ("Harvard Club de Chile", "hcchile", "Santiago, Chile"),
    ("Harvard Club of Guatemala", "hcguatemala", "Guatemala City, Guatemala"),
    ("Harvard Club of Peru", "hcperu", "Lima, Peru"),
    # ---- Middle East ----
    ("Harvard Club of Israel", "hcisrael", "Tel Aviv, Israel"),
    ("Harvard Club of Kuwait", "kuwait", "Kuwait City, Kuwait"),
    ("Harvard University Alumni Association of Lebanon", "huaalebanon", "Beirut, Lebanon"),
    ("Harvard Club of Saudi Arabia", "hcsaudiarabia", "Riyadh, Saudi Arabia"),
    ("Harvard Club of Turkey", "hcturkey", "Istanbul, Turkey"),
]

# --- Self-hosted clubs that emit schema.org Event JSON-LD --------------------- #
# (handled by the generic JSON-LD parser rather than the clubs.harvard.edu one).
JSONLD_SOURCES = [
    {"name": "Harvard Club of France", "url": "https://www.harvardclub.fr/events/",
     "city": "Paris, France"},
]

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def fetch(url, timeout=FETCH_TIMEOUT):
    """GET a page, returning text or None. Never raises."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Geocoding (free, no token) -- OpenStreetMap Nominatim with a persistent cache #
# --------------------------------------------------------------------------- #
_geocache = None


def _load_geocache():
    global _geocache
    if _geocache is None:
        try:
            _geocache = json.loads(GEOCODE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _geocache = {}
    return _geocache


def _save_geocache():
    try:
        GEOCODE_CACHE_FILE.write_text(json.dumps(_geocache), encoding="utf-8")
    except Exception as e:
        # Don't crash the request, but make a write failure visible: if the cache
        # never persists we'd re-query Nominatim every run and risk a rate-limit ban.
        print(f"WARNING: could not write geocode cache: {e}", file=sys.stderr)


def geocode(place):
    """Resolve a free-text place -> (lat, lon) via Nominatim, or (None, None).

    Cached forever on disk: a place only hits the network once, ever. Nominatim
    asks for <=1 req/sec, so we sleep briefly between live lookups. Never raises.
    """
    if not place:
        return (None, None)
    key = re.sub(r"\s+", " ", place.strip().lower())
    cache = _load_geocache()
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else (None, None)
    try:
        q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
        req = urllib.request.Request(
            "https://nominatim.openstreetmap.org/search?" + q,
            headers={"User-Agent": NOMINATIM_UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        time.sleep(1.1)  # be a good Nominatim citizen
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
            cache[key] = [lat, lon]
            _save_geocache()
            return (lat, lon)
        cache[key] = None  # remember misses too, so we don't re-query junk
        _save_geocache()
    except Exception:
        pass
    return (None, None)


# --------------------------------------------------------------------------- #
# clubs.harvard.edu platform parser (the bulk of the events)                  #
# --------------------------------------------------------------------------- #
def _clean_text(s, limit=400):
    if not s:
        return ""
    s = unescape(re.sub(r"<[^>]+>", " ", str(s)))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _abs(base, url):
    if not url:
        return base
    return urllib.parse.urljoin(base, url) if url.startswith("/") else url


def _iso_from_gcal(token):
    """'20260625T191500' -> '2026-06-25T19:15:00'; '20260625' -> '2026-06-25'."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?$", token or "")
    if not m:
        return ""
    y, mo, d, hh, mi, ss = m.groups()
    if hh is None:
        return f"{y}-{mo}-{d}"
    return f"{y}-{mo}-{d}T{hh}:{mi}:{ss}"


def _iso_from_text(date_text, time_text=None):
    """'Thu 25 Jun 2026' (+ optional '5:00PM') -> ISO string, or ''."""
    m = re.search(r"(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})", date_text or "")
    if not m:
        return ""
    day, mon, year = int(m.group(1)), _MONTHS.get(m.group(2)), int(m.group(3))
    if not mon:
        return ""
    hh = mm = 0
    if time_text:
        tm = re.match(r"(\d{1,2}):(\d{2})\s*([AP])M", time_text, re.I)
        if tm:
            hh, mm = int(tm.group(1)) % 12, int(tm.group(2))
            if tm.group(3).upper() == "P":
                hh += 12
    try:
        return datetime(year, mon, day, hh, mm).strftime(
            "%Y-%m-%dT%H:%M:%S" if time_text else "%Y-%m-%d")
    except ValueError:
        return ""


def events_from_clubs_harvard(html, name, base, home_city):
    """Parse upcoming events from a clubs.harvard.edu /events.html page."""
    out = []
    block = re.search(r'<ul class="med-image-list">(.*?)</ul>', html, re.S | re.I)
    if not block:
        return out
    # Each event is one <li>. Split on <li to get per-event chunks.
    for li in re.split(r"<li\b", block.group(1), flags=re.I)[1:]:
        h = re.search(r"<h4>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", li, re.S | re.I)
        if not h:
            continue
        title = _clean_text(h.group(2), 200)
        if not title:
            continue
        url = _abs(base, h.group(1))
        start = end = ""
        location = ""
        # Best source: the Google Calendar "add event" link (exact dates + address).
        g = re.search(r"calendar\.google\.com/calendar/r/eventedit\?([^\"'<>]+)", li)
        if g:
            q = urllib.parse.parse_qs(g.group(1).replace("&amp;", "&"))
            dates = q.get("dates", [""])[0]
            dm = re.match(r"([0-9T]+)/([0-9T]+)", dates)
            if dm:
                start, end = _iso_from_gcal(dm.group(1)), _iso_from_gcal(dm.group(2))
            location = (q.get("location", [""])[0] or "").strip().strip(",").strip()
        # Fallback: the visible "5:00PM - 6:30PM Thu 25 Jun 2026" line.
        if not start:
            p = re.search(
                r"<p>\s*(?:(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*\d{1,2}:\d{2}\s*[AP]M\s+)?"
                r"([A-Z][a-z]{2}\s+\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", li, re.I)
            if p:
                start = _iso_from_text(p.group(2), p.group(1))
        display = _clean_text(location, 200) or home_city
        out.append({
            "title": title,
            "start": start,
            "end": end,
            "location": display,
            "_geo_query": location or home_city,  # full address if we have it, else city
            "_geo_fallback": home_city,           # if the address won't geocode, pin to the city
            "lat": None,
            "lon": None,
            "url": url,
            "description": "",
            "image": None,
            "source": name,
        })
    return out


# --------------------------------------------------------------------------- #
# Generic schema.org Event JSON-LD parser (for self-hosted clubs)             #
# --------------------------------------------------------------------------- #
def _iter_jsonld(html):
    """Yield parsed objects from every <script type=application/ld+json> block."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(node, list):
                stack.extend(node)


def _location_strings(loc):
    """Best-effort (display, geocode-query) from a schema.org location value."""
    if not loc:
        return ("", "")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return (loc.strip(), loc.strip())
    if not isinstance(loc, dict):
        return ("", "")
    name = str(loc.get("name") or "").strip()
    addr = loc.get("address")
    parts = []
    if isinstance(addr, dict):
        for k in ("streetAddress", "addressLocality", "addressRegion",
                  "postalCode", "addressCountry"):
            v = addr.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("text")
            if v:
                parts.append(str(v).strip())
    elif isinstance(addr, str):
        parts.append(addr.strip())
    display = ", ".join([p for p in [name] + parts if p]) or name
    # For geocoding, skip the venue name (often un-geocodable) and use the address;
    # fall back to the display string if there's no structured address.
    geo_query = ", ".join(parts) if parts else display
    return (display, geo_query)


def _first_geo(loc):
    """Pull lat/lon straight out of the location if schema.org provided it."""
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return (None, None)
    geo = loc.get("geo")
    if isinstance(geo, dict):
        try:
            return (float(geo.get("latitude")), float(geo.get("longitude")))
        except (TypeError, ValueError):
            pass
    return (None, None)


def events_from_jsonld(html, source_name, base_url, home_city=""):
    """Pull events out of schema.org 'Event' JSON-LD (incl. subtypes)."""
    out = []
    for obj in _iter_jsonld(html):
        types = obj.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if not any(isinstance(t, str) and "Event" in t for t in types):
            continue
        title = _clean_text(obj.get("name"), 200)
        if not title:
            continue
        loc = obj.get("location")
        display, geo_query = _location_strings(loc)
        lat, lon = _first_geo(loc)
        url = obj.get("url") or obj.get("@id") or base_url
        if isinstance(url, str) and url.startswith("/"):
            url = urllib.parse.urljoin(base_url, url)
        image = obj.get("image")
        if isinstance(image, dict):
            image = image.get("url")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        out.append({
            "title": title,
            "start": obj.get("startDate") or "",
            "end": obj.get("endDate") or "",
            "location": display or home_city,
            "_geo_query": geo_query or home_city,
            "_geo_fallback": home_city,
            "lat": lat,
            "lon": lon,
            "url": url if isinstance(url, str) else base_url,
            "description": _clean_text(obj.get("description")),
            "image": image if isinstance(image, str) else None,
            "source": source_name,
        })
    return out


# --------------------------------------------------------------------------- #
# Per-source fetch + the public get_events()                                  #
# --------------------------------------------------------------------------- #
def _dedup(events):
    """Drop repeats within a list, keyed by (title, start)."""
    seen, uniq = set(), []
    for ev in events:
        key = (ev["title"], ev["start"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ev)
    return uniq


def fetch_club(club):
    """Fetch + parse one clubs.harvard.edu club. Returns (events, status). Never raises."""
    name, sub, home_city = club
    url = f"https://{sub}.clubs.harvard.edu/events.html"
    status = {"name": name, "url": url, "ok": False, "count": 0, "error": None}
    html = fetch(url)
    if not html:
        status["error"] = "could not fetch (timeout, blocked, or offline)"
        return [], status
    try:
        events = _dedup(events_from_clubs_harvard(html, name, url, home_city))
        status["ok"], status["count"] = True, len(events)
        return events, status
    except Exception as e:
        status["error"] = f"parse error: {e}"
        return [], status


def fetch_jsonld_source(src):
    """Fetch + parse one self-hosted JSON-LD club. Returns (events, status). Never raises."""
    status = {"name": src["name"], "url": src["url"], "ok": False, "count": 0, "error": None}
    html = fetch(src["url"])
    if not html:
        status["error"] = "could not fetch (timeout, blocked, or offline)"
        return [], status
    try:
        events = _dedup(events_from_jsonld(html, src["name"], src["url"], src.get("city", "")))
        status["ok"], status["count"] = True, len(events)
        return events, status
    except Exception as e:
        status["error"] = f"parse error: {e}"
        return [], status


def _read_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_age(cache):
    try:
        ts = datetime.fromisoformat(cache["fetchedAt"])
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def get_events(refresh=False):
    """Return {fetchedAt, events, sources, cached}. Uses the on-disk cache unless
    it's stale or refresh=True. Geocodes any event missing coordinates."""
    cache = _read_cache()
    if cache and not refresh:
        age = _cache_age(cache)
        if age is not None and age < CACHE_TTL_SECONDS:
            cache["cached"] = True
            cache["ageSeconds"] = int(age)
            return cache

    # Fetch every source concurrently -- a slow/blocked club can't stall the rest.
    all_events, statuses, seen = [], [], set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(fetch_club, CLUBS))
        results += list(ex.map(fetch_jsonld_source, JSONLD_SOURCES))
    for events, status in results:
        statuses.append(status)
        for ev in events:
            # Drop stale events: an "upcoming" page occasionally keeps an old item.
            # Keep undated events (some clubs list perpetual/ongoing ones).
            if ev["start"] and ev["start"][:10] < today:
                continue
            key = (ev["title"], ev["start"])  # dedup across sources
            if key in seen:
                continue
            seen.add(key)
            all_events.append(ev)

    # Geocode anything without coordinates (cached, so repeat runs are instant).
    # Many events share a home city, so this is far fewer Nominatim calls than events.
    # If a specific venue address won't resolve, fall back to the club's home city
    # so the event still lands on the map rather than vanishing.
    for ev in all_events:
        if ev.get("lat") is None or ev.get("lon") is None:
            lat, lon = geocode(ev.get("_geo_query") or ev.get("location", ""))
            fallback = ev.get("_geo_fallback")
            if lat is None and fallback and fallback != ev.get("_geo_query"):
                lat, lon = geocode(fallback)
            ev["lat"], ev["lon"] = lat, lon
        ev.pop("_geo_query", None)
        ev.pop("_geo_fallback", None)

    all_events.sort(key=lambda e: (not e.get("start"), e.get("start") or ""))
    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "events": all_events,
        "sources": statuses,
        "cached": False,
        "ageSeconds": 0,
    }
    try:
        CACHE_FILE.write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass
    return result


if __name__ == "__main__":
    # Quick manual test: `py harvard_events_scraper.py`
    data = get_events(refresh=True)
    ok = sum(1 for s in data["sources"] if s["ok"])
    print(f"Fetched {len(data['events'])} event(s) from {ok}/{len(data['sources'])} sources OK")
    for s in sorted(data["sources"], key=lambda s: -s["count"]):
        if s["count"] or s["error"]:
            tag = "OK" if s["ok"] else "FAIL"
            extra = f"  {s['error']}" if s["error"] else ""
            print(f"  - {s['name']}: {tag} ({s['count']}){extra}")
    print("--- sample events ---")
    for ev in data["events"][:20]:
        where = ev["location"] or "?"
        pin = f"({ev['lat']:.2f},{ev['lon']:.2f})" if ev["lat"] is not None else "(no geo)"
        line = f"  {ev['start'][:10] or '????-??-??'}  {pin:>18}  {ev['title'][:48]}  @ {where[:38]}"
        # Windows consoles are often cp1252; don't let an emoji/accent crash the test.
        print(line.encode(sys.stdout.encoding or "utf-8", "replace").decode(
            sys.stdout.encoding or "utf-8", "replace"))
