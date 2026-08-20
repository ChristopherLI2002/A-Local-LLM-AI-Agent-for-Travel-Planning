"""Lookup cover photos for timetable rows (Wikipedia, Openverse, LoremFlickr)."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_UA = (
    "LocalLLMTravelAgent/1.0 (educational travel planner; "
    "+https://github.com/local/local-llm-travel-agent) Python-urllib"
)
_ALLOWED_THUMB_WIDTHS = (500, 330, 250, 120)
_FETCH_LOCK = threading.Lock()
_LAST_FETCH_AT = 0.0
_MIN_FETCH_GAP_S = 0.35
_DISK_CACHE = Path.home() / ".cache" / "local_llm_travel_agent" / "thumbs"
_LOOKUP_CACHE: dict[str, str] = {}
_OPENVERSE_CACHE: dict[str, str] = {}
# Trip.com attraction detail covers (name -> image URL), set after scrape
_TRIP_ATTRACTION_IMAGES: dict[str, str] = {}


def set_trip_attraction_images(cards: list[dict[str, str]] | None) -> None:
    """Register scraped Trip.com attraction cover photos for timetable rows."""
    _TRIP_ATTRACTION_IMAGES.clear()
    for card in cards or []:
        name = re.sub(r"\s+", " ", (card.get("name") or "").strip()).lower()
        url = (card.get("image_url") or "").strip()
        if name and url.startswith("http") and "tripcdn" in url.lower():
            _TRIP_ATTRACTION_IMAGES[name] = url


def lookup_trip_attraction_image(detail: str) -> str:
    """Match a timetable line to a scraped Trip.com attraction cover."""
    if not _TRIP_ATTRACTION_IMAGES:
        return ""
    low = re.sub(r"\s+", " ", (detail or "").strip()).lower()
    low = re.sub(r"\s*\([^)]*(?:open|hours?|\d\s*[-–]\s*\d)[^)]*\)\s*$", "", low)
    if not low:
        return ""
    if low in _TRIP_ATTRACTION_IMAGES:
        return _TRIP_ATTRACTION_IMAGES[low]
    for name, url in _TRIP_ATTRACTION_IMAGES.items():
        if name in low or low in name:
            return url
    return ""


# Keyword in timetable text -> Wikipedia page title
_WIKI_TITLES: dict[str, str] = {
    "shinjuku gyoen": "Shinjuku Gyoen",
    "shinjuku": "Shinjuku",
    "metropolitan government": "Tokyo Metropolitan Government Building",
    "meiji jingu": "Meiji Shrine",
    "meiji shrine": "Meiji Shrine",
    "takeshita": "Takeshita Street",
    "harajuku": "Harajuku",
    "shibuya scramble": "Shibuya Crossing",
    "shibuya sky": "Shibuya Scramble Square",
    "shibuya": "Shibuya",
    "senso-ji": "Sensō-ji",
    "sensoji": "Sensō-ji",
    "tokyo skytree": "Tokyo Skytree",
    "skytree": "Tokyo Skytree",
    "akihabara": "Akihabara",
    "imperial palace": "Tokyo Imperial Palace",
    "ginza": "Ginza",
    "teamlab": "teamLab Borderless",
    "odaiba": "Odaiba",
    "divercity": "DiverCity Tokyo Plaza",
    "kamakura": "Kamakura",
    "great buddha": "Kōtoku-in",
    "kotokuin": "Kōtoku-in",
    "hasedera": "Hase-dera (Kamakura)",
    "gyeongbokgung": "Gyeongbokgung",
    "bukchon": "Bukchon Hanok Village",
    "insadong": "Insadong",
    "n seoul tower": "N Seoul Tower",
    "namsan": "N Seoul Tower",
    "myeongdong": "Myeongdong",
    "hongdae": "Hongdae",
    "coex": "COEX Mall",
    "seongsu": "Seongsu-dong",
    "dotonbori": "Dōtonbori",
    "osaka castle": "Osaka Castle",
    "umeda sky": "Umeda Sky Building",
    "todai-ji": "Tōdai-ji",
    "nara park": "Nara Park",
    "notre-dame": "Notre-Dame de Paris",
    "sainte-chapelle": "Sainte-Chapelle",
    "louvre": "Louvre",
    "eiffel": "Eiffel Tower",
    "arc de triomphe": "Arc de Triomphe",
    "sacre-coeur": "Sacré-Cœur, Paris",
    "montmartre": "Montmartre",
    "wat arun": "Wat Arun",
    "grand palace": "Grand Palace, Bangkok",
    "wat pho": "Wat Pho",
    "jim thompson": "Jim Thompson House",
    "gardens by the bay": "Gardens by the Bay",
    "marina bay sands": "Marina Bay Sands",
    "merlion": "Merlion",
    "taipei 101": "Taipei 101",
    "chiang kai-shek": "Chiang Kai-shek Memorial Hall",
    "jiufen": "Jiufen",
    "elephant mountain": "Elephant Mountain (Taipei)",
    "victoria peak": "Victoria Peak",
    "big buddha": "Tian Tan Buddha",
    "ngong ping": "Ngong Ping",
    "avenue of stars": "Avenue of Stars, Hong Kong",
    "man mo temple": "Man Mo Temple",
    "london eye": "London Eye",
    "borough market": "Borough Market",
    "westminster": "Palace of Westminster",
    "big ben": "Big Ben",
    "british museum": "British Museum",
    "buckingham": "Buckingham Palace",
    "tower of london": "Tower of London",
    "tower bridge": "Tower Bridge",
    "sky garden": "Sky Garden",
    "tate modern": "Tate Modern",
    "golden gate": "Golden Gate Bridge",
    "alcatraz": "Alcatraz Island",
    "fisherman's wharf": "Fisherman's Wharf",
    "pier 39": "Pier 39",
    "ferry building": "Ferry Building",
    "painted ladies": "Painted Ladies",
    "palace of fine arts": "Palace of Fine Arts",
    "chinatown": "Chinatown, San Francisco",
    # Meals / stays / transit cues
    "ichiran": "Ramen",
    "ramen": "Ramen",
    "gyoza": "Gyoza",
    "sushi": "Sushi",
    "yakitori": "Yakitori",
    "omoide yokocho": "Omoide Yokocho",
    "okonomiyaki": "Okonomiyaki",
    "takoyaki": "Takoyaki",
    "narita": "Narita International Airport",
    "haneda": "Haneda Airport",
    "land at airport": "Airport terminal",
    "transfer to airport": "Airport terminal",
    "flight departs": "Airplane",
    "airport": "Airport terminal",
    "hotel check-in": "Capsule hotel",
    "hotel checkout": "Capsule hotel",
    "wake up": "Capsule hotel",
    "neighborhood walk": "Street",
    "free time": "Café",
    "coffee": "Coffee",
    "don quijote": "Don Quijote (store)",
    "souvenir": "Souvenir",
    "bento": "Bento",
    "depachika": "Depachika",
}

# Category fallbacks when no specific title matches
_CATEGORY_FALLBACKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(land at airport|transfer to airport|airport|narita|haneda|nrt|hnd|icn|cdg)\b"), "Airport terminal"),
    (re.compile(r"(?i)\b(flight departs|airplane|boarding)\b"), "Airplane"),
    (re.compile(r"(?i)\b(hotel|check-in|checkout|check out|wake up|pack)\b"), "Capsule hotel"),
    (re.compile(r"(?i)\b(ramen|ichiran)\b"), "Ramen"),
    (re.compile(r"(?i)\b(sushi|uobei)\b"), "Sushi"),
    (re.compile(r"(?i)\b(yakitori|omoide)\b"), "Yakitori"),
    (re.compile(r"(?i)\b(gyoza)\b"), "Gyoza"),
    (re.compile(r"(?i)\b(okonomiyaki)\b"), "Okonomiyaki"),
    (re.compile(r"(?i)\b(lunch|dinner|restaurant|food|meal)\b"), "Japanese cuisine"),
    (re.compile(r"(?i)\b(coffee|cafe|café|free time)\b"), "Café"),
    (re.compile(r"(?i)\b(walk|neighborhood)\b"), "Street"),
    (re.compile(r"(?i)\b(souvenir|don quijote|depachika)\b"), "Souvenir"),
]

_CITY_FALLBACKS: dict[str, str] = {
    "tokyo": "Tokyo",
    "seoul": "Seoul",
    "osaka": "Osaka",
    "paris": "Paris",
    "bangkok": "Bangkok",
    "singapore": "Singapore",
    "taipei": "Taipei",
    "hong kong": "Hong Kong",
}

# Prefer real terminal / runway photos — never the generic "Airport" diagram page
_CITY_AIRPORTS: dict[str, str] = {
    "tokyo": "Narita International Airport",
    "shinjuku": "Narita International Airport",
    "seoul": "Incheon International Airport",
    "osaka": "Kansai International Airport",
    "paris": "Charles de Gaulle Airport",
    "bangkok": "Suvarnabhumi Airport",
    "singapore": "Changi Airport",
    "taipei": "Taiwan Taoyuan International Airport",
    "hong kong": "Hong Kong International Airport",
    "london": "Heathrow Airport",
    "san francisco": "San Francisco International Airport",
    "california": "San Francisco International Airport",
}

_AIRPORT_TEXT = re.compile(
    r"(?i)\b(airport|narita|haneda|incheon|kansai|changi|suvarnabhumi|"
    r"land at airport|transfer to airport|nrt|hnd|icn|cdg|boarding gate)\b"
)

_BAD_IMAGE_URL = re.compile(
    r"(?i)(infrastructure|diagram|schematic|floor[_-]?plan|map_of|blueprint|pictogram)"
)

_WIKI_CACHE: dict[str, str] = {}


def _title_lookup(text: str) -> str:
    low = (text or "").lower()
    best = ""
    best_len = 0
    for key, title in _WIKI_TITLES.items():
        if key in low and len(key) > best_len:
            best = title
            best_len = len(key)
    return best


def _category_title(text: str) -> str:
    for pat, title in _CATEGORY_FALLBACKS:
        if pat.search(text or ""):
            return title
    return ""


def _wiki_title_from_text(text: str) -> str:
    """Pick a Wikipedia page title from a timetable line."""
    chunk = (text or "").split(",")[0].split("(")[0].split("+")[0].strip()
    chunk = re.sub(r"\s+then\s+.*", "", chunk, flags=re.I)
    chunk = re.sub(r"\s+and\s+.*", "", chunk, flags=re.I)
    chunk = re.sub(
        r"(?i)^(visit|explore|arrive at|go to|light|land at)\s+",
        "",
        chunk,
    ).strip()
    return chunk


def _request_json(url: str, *, timeout: float = 10) -> dict:
    global _LAST_FETCH_AT
    with _FETCH_LOCK:
        gap = time.monotonic() - _LAST_FETCH_AT
        if gap < _MIN_FETCH_GAP_S:
            time.sleep(_MIN_FETCH_GAP_S - gap)
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _LAST_FETCH_AT = time.monotonic()
        return data


def _wikipedia_thumbnail(title: str) -> str:
    if not title:
        return ""
    if title in _WIKI_CACHE:
        return _WIKI_CACHE[title]
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
    # MediaWiki pageimages API picks an allowed thumb width for us.
    api = (
        "https://en.wikipedia.org/w/api.php?"
        + urllib.parse.urlencode(
            {
                "action": "query",
                "titles": title,
                "prop": "pageimages",
                "format": "json",
                "pithumbsize": 500,
                "pilicense": "any",
                "redirects": 1,
            }
        )
    )
    try:
        data = _request_json(api)
        pages = ((data.get("query") or {}).get("pages") or {})
        for page in pages.values():
            if not isinstance(page, dict) or page.get("missing") is not None:
                continue
            thumb = ((page.get("thumbnail") or {}).get("source") or "").strip()
            thumb = _acceptable_image_url(sanitize_image_url(thumb) if thumb else "")
            if thumb:
                _WIKI_CACHE[title] = thumb
                return thumb
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError, TypeError):
        pass

    # Fallback: REST summary endpoint
    try:
        summary = _request_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}")
        thumb = ((summary.get("thumbnail") or {}).get("source") or "").strip()
        original = ((summary.get("originalimage") or {}).get("source") or "").strip()
        chosen = thumb if thumb.startswith("http") else original
        chosen = _acceptable_image_url(sanitize_image_url(chosen) if chosen else "")
        if chosen:
            _WIKI_CACHE[title] = chosen
            return chosen
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        pass
    _WIKI_CACHE[title] = ""
    return ""


def sanitize_image_url(url: str, *, width: int = 500) -> str:
    """Fix Wikimedia thumb URLs that use disallowed pixel widths (HTTP 400)."""
    url = (url or "").strip()
    if not url:
        return ""
    if width not in _ALLOWED_THUMB_WIDTHS:
        width = 500
    # Allowed steps: 20,40,60,120,250,330,500,960,... — never invent other widths.
    if "upload.wikimedia.org" in url and "/thumb/" in url:
        url = re.sub(r"/\d+px-", f"/{width}px-", url)
    return url


def _thumb_url_variants(url: str) -> list[str]:
    """Candidate URLs with allowed Wikimedia widths (and the original)."""
    url = (url or "").strip()
    if not url:
        return []
    # Non-Wikimedia hosts: fetch as-is only
    if "upload.wikimedia.org" not in url:
        return [url]
    out: list[str] = []
    seen: set[str] = set()
    for w in _ALLOWED_THUMB_WIDTHS:
        cand = sanitize_image_url(url, width=w)
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    if url not in seen:
        out.append(url)
    return out


def fetch_image_bytes(url: str, *, retries: int = 4) -> bytes:
    """Download image bytes with disk cache, rate limiting, and 429 retries."""
    global _LAST_FETCH_AT
    url = sanitize_image_url(url) if "upload.wikimedia.org" in (url or "") else (url or "").strip()
    if not url.startswith("http"):
        return b""

    _DISK_CACHE.mkdir(parents=True, exist_ok=True)
    variants = _thumb_url_variants(url)
    # Prefer cache hit for any variant
    for cand in variants:
        key = hashlib.sha1(cand.encode("utf-8")).hexdigest()
        path = _DISK_CACHE / f"{key}.img"
        if path.exists() and path.stat().st_size > 200:
            try:
                return path.read_bytes()
            except OSError:
                pass

    host = urllib.parse.urlparse(url).netloc.lower()
    referer = "https://en.wikipedia.org/"
    if "flickr" in host or "staticflickr" in host:
        referer = "https://www.flickr.com/"
    elif "openverse" in host:
        referer = "https://openverse.org/"
    elif "loremflickr" in host:
        referer = "https://loremflickr.com/"
    elif "tripcdn" in host or "trip.com" in host:
        referer = "https://hk.trip.com/"

    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        for cand in variants:
            key = hashlib.sha1(cand.encode("utf-8")).hexdigest()
            path = _DISK_CACHE / f"{key}.img"
            try:
                with _FETCH_LOCK:
                    gap = time.monotonic() - _LAST_FETCH_AT
                    if gap < _MIN_FETCH_GAP_S:
                        time.sleep(_MIN_FETCH_GAP_S - gap)
                    req = urllib.request.Request(
                        cand,
                        headers={
                            "User-Agent": _UA,
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                            "Referer": referer,
                        },
                    )
                    with urllib.request.urlopen(req, timeout=14) as resp:
                        data = resp.read()
                        # Follow LoremFlickr / CDN redirects already resolved by urlopen
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                    _LAST_FETCH_AT = time.monotonic()
                if data and len(data) > 200 and (
                    "image" in ctype
                    or (not ctype)
                    or data[:3] in (b"\xff\xd8\xff", b"\x89PN", b"GIF")
                ):
                    if "html" in ctype or "json" in ctype or "text/" in ctype:
                        continue
                    try:
                        path.write_bytes(data)
                    except OSError:
                        pass
                    return data
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    time.sleep(1.2 * (attempt + 1))
                    break
                if e.code == 400:
                    continue
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                continue
        time.sleep(0.4 * (attempt + 1))
    if last_err:
        return b""
    return b""


def _is_airport_query(text: str) -> bool:
    return bool(_AIRPORT_TEXT.search(text or ""))


def _city_airport_title(city: str) -> str:
    low = (city or "").strip().lower()
    for name, title in _CITY_AIRPORTS.items():
        if name in low or low in name:
            return title
    return ""


def _acceptable_image_url(url: str) -> str:
    """Drop diagram / schematic / map-style assets (e.g. Airport infrastructure)."""
    url = (url or "").strip()
    if not url.startswith("http"):
        return ""
    if _BAD_IMAGE_URL.search(url):
        return ""
    if "Airport_infrastructure" in url:
        return ""
    return url


def _search_phrases(text: str, city: str = "") -> list[str]:
    """Build ordered search phrases for multi-source image lookup."""
    phrases: list[str] = []

    # Airports: use a real photo subject, never the generic "Airport" diagram page
    if _is_airport_query(text):
        apt = _city_airport_title(city)
        if apt:
            phrases.append(apt)
        phrases.extend(
            [
                "Airport terminal",
                "Jet bridge",
                "Airplane at airport gate",
                "airport terminal departure hall",
            ]
        )

    specific = _title_lookup(text)
    if specific and specific.lower() != "airport":
        phrases.append(specific)
    cat = _category_title(text)
    if cat and cat.lower() != "airport" and cat not in phrases:
        phrases.append(cat)
    parsed = _wiki_title_from_text(text)
    if (
        parsed
        and parsed not in phrases
        and len(parsed) > 2
        and parsed.lower() != "airport"
    ):
        phrases.append(parsed)
    # Short keyword bag from the activity line
    cleaned = re.sub(r"[()\[\]{}]", " ", text or "")
    cleaned = re.sub(r"(?i)\b(near|after|then|and|or|the|a|an|to|at|in|of|for|with)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned and cleaned.lower() not in {p.lower() for p in phrases}:
        phrases.append(cleaned[:80])
    city = (city or "").strip()
    if city:
        enriched: list[str] = []
        for p in phrases:
            if city.lower() not in p.lower():
                enriched.append(f"{p} {city}")
            enriched.append(p)
        phrases = enriched
        if city not in phrases:
            phrases.append(city)
    # Deduplicate preserving order
    out: list[str] = []
    seen: set[str] = set()
    for p in phrases:
        key = p.strip().lower()
        if len(key) < 2 or key in seen or key == "airport":
            continue
        seen.add(key)
        out.append(p.strip())
    return out or ["travel"]


def _image_identity(url: str) -> str:
    """Fingerprint so same photo at different sizes/hosts counts as one image."""
    raw = (url or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    path = low.split("?", 1)[0]
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r"^\d+px-", "", name)
    # Flickr: 8666784025_0acf8b0fb1_b.jpg -> flickr:8666784025
    fm = re.match(r"^(\d+)_[0-9a-f]+", name)
    if fm or "staticflickr.com" in low or "flickr.com" in low:
        if fm:
            return f"flickr:{fm.group(1)}"
        ids = re.findall(r"/(\d{6,})_", path)
        if ids:
            return f"flickr:{ids[-1]}"
    # LoremFlickr locks are unique per query string
    if "loremflickr.com" in low:
        return low
    # Wikimedia / generic: basename without size prefix
    if name:
        return f"file:{urllib.parse.unquote(name)}"
    return path


def _openverse_images(query: str, *, limit: int = 6) -> list[str]:
    """Search Openverse; return several direct image URLs."""
    q = (query or "").strip()
    if not q:
        return []
    cache_key = f"{q}|{limit}"
    if cache_key in _OPENVERSE_CACHE:
        cached = _OPENVERSE_CACHE[cache_key]
        return [u for u in cached.split("\n") if u] if cached else []

    api = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(
        {
            "q": q,
            "page_size": max(5, limit),
            "mature": "false",
            "category": "photograph",
            "format": "json",
        }
    )
    out: list[str] = []
    seen: set[str] = set()
    try:
        data = _request_json(api, timeout=12)
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            for cand in ((item.get("url") or "").strip(), (item.get("thumbnail") or "").strip()):
                if not cand.startswith("http"):
                    continue
                low = cand.lower()
                if low.endswith(".svg") or "svg+" in low:
                    continue
                ident = _image_identity(cand)
                if not ident or ident in seen:
                    continue
                seen.add(ident)
                out.append(cand)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError, TypeError):
        pass
    _OPENVERSE_CACHE[cache_key] = "\n".join(out)
    # Keep legacy single-query cache key compatible
    _OPENVERSE_CACHE[q] = out[0] if out else ""
    return out


def _openverse_image(query: str) -> str:
    """Search Openverse (Flickr / CC photos). Returns a direct image URL."""
    urls = _openverse_images(query, limit=1)
    return urls[0] if urls else ""


def _loremflickr_image(query: str, *, width: int = 480, height: int = 320) -> str:
    """Deterministic stock photo URL from LoremFlickr tags (always available)."""
    tags = re.sub(r"[^\w\s,-]", " ", query or "travel")
    tags = re.sub(r"\s+", ",", tags.strip())
    tags = re.sub(r",+", ",", tags).strip(",") or "travel"
    # lock=seed keeps the same image for the same activity across reloads
    seed = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    return f"https://loremflickr.com/{width}/{height}/{urllib.parse.quote(tags)}/all?lock={seed}"


def candidate_image_urls(
    text: str,
    city: str = "",
    *,
    limit: int = 6,
    exclude: set[str] | None = None,
) -> list[str]:
    """Ordered image URL candidates across Wikipedia, Openverse, and LoremFlickr."""
    phrases = _search_phrases(text, city)
    urls: list[str] = []
    seen: set[str] = set(exclude or ())

    def _add(u: str) -> None:
        u = _acceptable_image_url(sanitize_image_url(u) if "upload.wikimedia.org" in (u or "") else u)
        if not u:
            return
        ident = _image_identity(u)
        if not ident or ident in seen:
            return
        seen.add(ident)
        urls.append(u)

    airport = _is_airport_query(text)
    # For airports, prefer photographic sources first (Wiki "Airport" is a diagram)
    if airport:
        for phrase in phrases[:5]:
            for ov in _openverse_images(phrase, limit=4):
                _add(ov)
                if len(urls) >= limit:
                    return urls[:limit]
        for phrase in phrases[:5]:
            _add(_wikipedia_thumbnail(phrase))
            if len(urls) >= limit:
                return urls[:limit]
        _add(_loremflickr_image(f"airport,terminal,airplane,{text[:40]}"))
    else:
        for phrase in phrases[:5]:
            _add(_wikipedia_thumbnail(phrase))
            if len(urls) >= limit:
                return urls[:limit]
        for phrase in phrases[:5]:
            for ov in _openverse_images(phrase, limit=4):
                _add(ov)
                if len(urls) >= limit:
                    return urls[:limit]
        _add(_loremflickr_image(f"{phrases[0] if phrases else (city or 'travel')}|{text[:48]}"))
    return urls[:limit]


def lookup_image(text: str, city: str = "", *, exclude: set[str] | None = None) -> str:
    """Return a photo URL from Trip.com covers, Wikipedia, Openverse, or LoremFlickr."""
    excl = exclude or set()
    trip = lookup_trip_attraction_image(text)
    if trip and _image_identity(trip) not in excl:
        return trip
    cache_key = f"{(city or '').strip().lower()}|{(text or '').strip().lower()}"
    if not excl and cache_key in _LOOKUP_CACHE:
        cached = _LOOKUP_CACHE[cache_key]
        if cached and _image_identity(cached) not in excl:
            return cached
    urls = candidate_image_urls(text, city, limit=8, exclude=excl)
    url = urls[0] if urls else _loremflickr_image(f"{city or 'travel'}|{text}|{len(excl)}")
    if not excl:
        _LOOKUP_CACHE[cache_key] = url
    return url


def images_for_timetable(body: str, city: str = "") -> dict[str, str]:
    """Map every timetable HH:MM row to a distinct photo URL."""
    out: dict[str, str] = {}
    used: set[str] = set()

    for line in (body or "").splitlines():
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)\s+(.*)$", line.strip())
        if not m:
            continue
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
        detail = m.group(3).strip()
        url = ""
        trip = lookup_trip_attraction_image(detail)
        if trip and _image_identity(trip) not in used:
            url = trip
            used.add(_image_identity(trip))
        if not url:
            for cand in candidate_image_urls(detail, city, limit=10, exclude=used):
                ident = _image_identity(cand)
                if ident and ident not in used:
                    url = cand
                    used.add(ident)
                    break
        if not url:
            url = _loremflickr_image(f"{city}|{t}|{detail}")
            used.add(_image_identity(url))
        out[t] = url
    return out


def enrich_block_images(block: object, city: str = "") -> None:
    """Attach / refresh images so every timed row has a photo URL."""
    body = getattr(block, "body", "") or ""
    if not body or not hasattr(block, "images"):
        return
    imgs = images_for_timetable(body, city)
    if imgs:
        block.images = imgs  # type: ignore[attr-defined]
