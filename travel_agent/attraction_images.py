"""Lookup cover photos for timetable attractions (Wikipedia thumbnails)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

# Keyword in timetable text -> Wikipedia page title
_WIKI_TITLES: dict[str, str] = {
    "shinjuku gyoen": "Shinjuku Gyoen",
    "metropolitan government": "Tokyo Metropolitan Government Building",
    "meiji jingu": "Meiji Shrine",
    "meiji shrine": "Meiji Shrine",
    "takeshita": "Takeshita Street",
    "harajuku": "Harajuku",
    "shibuya scramble": "Shibuya Crossing",
    "shibuya sky": "Shibuya Scramble Square",
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
}

_MEAL_TRANSIT_RE = re.compile(
    r"(?i)\b("
    r"ramen|sushi|gyoza|izakaya|bento|restaurant|lunch|dinner|breakfast|brunch|"
    r"cafe|coffee|food hall|food floor|market food|snack|depachika|"
    r"metro|mtr|mrt| train|line:| bus|limousine|express|airport|check-in|checkout|"
    r"depart|arrive|transfer|return train|free time|don quijote|souvenir"
    r")\b"
)

_WIKI_CACHE: dict[str, str] = {}


def is_meal_or_transit(text: str) -> bool:
    return bool(_MEAL_TRANSIT_RE.search(text or ""))


def _title_lookup(text: str) -> str:
    low = (text or "").lower()
    best = ""
    best_len = 0
    for key, title in _WIKI_TITLES.items():
        if key in low and len(key) > best_len:
            best = title
            best_len = len(key)
    return best


def _wiki_title_from_text(text: str) -> str:
    """Pick a Wikipedia page title from a timetable line."""
    chunk = (text or "").split(",")[0].split("(")[0].split("+")[0].strip()
    chunk = re.sub(r"\s+then\s+.*", "", chunk, flags=re.I)
    chunk = re.sub(r"\s+and\s+.*", "", chunk, flags=re.I)
    chunk = re.sub(r"(?i)^(visit|explore|arrive at|go to)\s+", "", chunk).strip()
    return chunk


def _wikipedia_thumbnail(title: str) -> str:
    if not title:
        return ""
    if title in _WIKI_CACHE:
        return _WIKI_CACHE[title]
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "VoyageTravelAgent/1.0 (itinerary thumbnails)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        thumb = ((data.get("thumbnail") or {}).get("source") or "").strip()
        if thumb.startswith("http"):
            _WIKI_CACHE[title] = thumb
            return thumb
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        pass
    _WIKI_CACHE[title] = ""
    return ""


def lookup_image(text: str, city: str = "") -> str:
    """Return a photo URL for an attraction line, or empty string."""
    if is_meal_or_transit(text):
        return ""
    title = _title_lookup(text) or _wiki_title_from_text(text)
    if title:
        wiki = _wikipedia_thumbnail(title)
        if wiki:
            return wiki
    if city and title:
        wiki = _wikipedia_thumbnail(f"{title}, {city}")
        if wiki:
            return wiki
    return ""


_TRANSIT_ONLY_RE = re.compile(
    r"(?i)^(jr |tokyo metro|mtr |metro |airport|depart|arrive|return train|hotel check|last souvenir)"
)


def images_for_timetable(body: str, city: str = "") -> dict[str, str]:
    """Map timetable times (HH:MM) to attraction photo URLs."""
    out: dict[str, str] = {}
    seen_urls: set[str] = set()
    for line in (body or "").splitlines():
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)\s+(.*)$", line.strip())
        if not m:
            continue
        t = f"{int(m.group(1)):02d}:{m.group(2)}"
        detail = m.group(3).strip()
        if is_meal_or_transit(detail) or _TRANSIT_ONLY_RE.match(detail):
            continue
        url = lookup_image(detail, city)
        if url and url not in seen_urls:
            out[t] = url
            seen_urls.add(url)
    return out


def enrich_block_images(block: object, city: str = "") -> None:
    """Attach images dict to a day Block when missing."""
    body = getattr(block, "body", "") or ""
    current = getattr(block, "images", None)
    if current and len(current) > 0:
        return
    if not body:
        return
    imgs = images_for_timetable(body, city)
    if imgs and hasattr(block, "images"):
        block.images = imgs  # type: ignore[attr-defined]
