"""Price extraction and comparison helpers for Trip.com page text."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

# HK$1,234 / HKD 1234 / $1,234.00 / from HK$806
_PRICE_RE = re.compile(
    r"(?:from\s*)?(?:HK\$|HKD\s*|\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|"
    r"[0-9]+(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

_AIRLINE_HINTS = (
    "cathay",
    "hong kong airlines",
    "china airlines",
    "eva air",
    "japan airlines",
    "ana",
    "singapore airlines",
    "thai",
    "korean air",
    "asiana",
    "peach",
    "scoot",
    "jetstar",
    "emirates",
    "qatar",
    "cx",
    "ka",
    "br",
    "ci",
)


def parse_prices(text: str) -> list[float]:
    prices: list[float] = []
    for match in _PRICE_RE.finditer(text or ""):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        # Ignore tiny/noise and absurd values
        if 50 <= value <= 500_000:
            prices.append(value)
    return prices


def unique_sorted_prices(prices: list[float], limit: int = 12) -> list[float]:
    seen: set[float] = set()
    out: list[float] = []
    for p in sorted(prices):
        key = round(p, 2)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


def nearby_dates(center: str, offsets: tuple[int, ...] = (-3, 0, 3)) -> list[str]:
    """Return future ISO dates around a center date."""
    try:
        base = date.fromisoformat(center)
    except ValueError:
        return [center]
    today = date.today()
    out: list[str] = []
    for off in offsets:
        d = base + timedelta(days=off)
        if d >= today:
            out.append(d.isoformat())
    # Always include center if valid/future
    if base >= today and center not in out:
        out.insert(len(out) // 2, center)
    return out[:4] or [center]


def extract_priced_snippets(text: str, limit: int = 5) -> list[str]:
    """Pull short lines that look like hotel/flight options with prices."""
    snippets: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.split())
        if len(line) < 12 or len(line) > 160:
            continue
        prices = parse_prices(line)
        if not prices:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        low = min(prices)
        snippets.append(f"{line}  (from HK${low:,.0f})")
        if len(snippets) >= limit:
            break
    return snippets


def summarize_prices(label: str, text: str, url: str = "") -> dict[str, Any]:
    prices = unique_sorted_prices(parse_prices(text))
    summary: dict[str, Any] = {
        "label": label,
        "url": url,
        "price_count": len(prices),
        "prices_hkd": prices,
        "lowest_hkd": prices[0] if prices else None,
        "highest_hkd": prices[-1] if prices else None,
        "median_hkd": prices[len(prices) // 2] if prices else None,
        "snippets": extract_priced_snippets(text),
    }
    return summary


def format_comparison_table(rows: list[dict[str, Any]], title: str) -> str:
    lines = [title, "=" * len(title)]
    ranked = sorted(
        rows,
        key=lambda r: (r.get("lowest_hkd") is None, r.get("lowest_hkd") or 0),
    )
    if not ranked:
        return title + "\nNo comparable prices found."

    for i, row in enumerate(ranked, 1):
        low = row.get("lowest_hkd")
        med = row.get("median_hkd")
        high = row.get("highest_hkd")
        count = row.get("price_count", 0)
        low_s = f"HK${low:,.0f}" if low is not None else "n/a"
        med_s = f"HK${med:,.0f}" if med is not None else "n/a"
        high_s = f"HK${high:,.0f}" if high is not None else "n/a"
        badge = " <- CHEAPEST" if i == 1 and low is not None else ""
        lines.append(
            f"{i}. {row.get('label')}: lowest {low_s} | median {med_s} | "
            f"highest {high_s} ({count} prices){badge}"
        )
        if row.get("url"):
            lines.append(f"   URL: {row['url']}")
        sample = row.get("prices_hkd") or []
        if sample:
            sample_s = ", ".join(f"HK${p:,.0f}" for p in sample[:6])
            lines.append(f"   Sample: {sample_s}")

    with_price = [r for r in ranked if r.get("lowest_hkd") is not None]
    if len(with_price) >= 2:
        best = with_price[0]["lowest_hkd"]
        worst = with_price[-1]["lowest_hkd"]
        if best and worst and worst > best:
            save = worst - best
            pct = (save / worst) * 100
            lines.append("")
            lines.append(
                f"Savings vs most expensive option: HK${save:,.0f} ({pct:.0f}% lower)."
            )
    return "\n".join(lines)


def nights_between(checkin: str, checkout: str) -> int:
    try:
        a = date.fromisoformat(checkin)
        b = date.fromisoformat(checkout)
        return max(1, (b - a).days)
    except ValueError:
        return 1


def pick_cheapest_from_comparison(text: str) -> dict[str, Any]:
    """Parse a comparison table and return the cheapest row's label/price/url."""
    result: dict[str, Any] = {
        "label": "",
        "lowest_hkd": None,
        "url": "",
        "depart_date": "",
        "checkin": "",
    }
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if "CHEAPEST" not in line:
            continue
        result["label"] = line.strip()
        price_m = re.search(r"lowest HK\$([0-9,]+(?:\.[0-9]+)?)", line, re.I)
        if price_m:
            try:
                result["lowest_hkd"] = float(price_m.group(1).replace(",", ""))
            except ValueError:
                pass
        depart_m = re.search(r"depart (\d{4}-\d{2}-\d{2})", line, re.I)
        if depart_m:
            result["depart_date"] = depart_m.group(1)
        checkin_m = re.search(
            r"\|\s*(\d{4}-\d{2}-\d{2})\s*->",
            line,
        )
        if checkin_m:
            result["checkin"] = checkin_m.group(1)
        # URL is usually on the next non-empty line
        for j in range(i + 1, min(i + 4, len(lines))):
            url_m = re.search(r"https?://\S+", lines[j])
            if url_m:
                result["url"] = url_m.group(0).rstrip(".,;")
                break
        break
    return result
