"""Turn agent plan text into safe, displayable HTML."""

from __future__ import annotations

import html
import re
from typing import Any

import bleach
import markdown

ALLOWED_TAGS = [
    "article",
    "section",
    "header",
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "strong",
    "em",
    "b",
    "i",
    "br",
    "hr",
    "a",
    "blockquote",
    "code",
    "pre",
    "span",
    "div",
]

ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target", "class"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "*": ["class"],
}

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_FLIGHT_LABEL_RE = re.compile(
    r"(?:Flight search URL|Flights?[^\n]{0,40}Search URL)\s*:\s*(https?://\S+)",
    re.IGNORECASE,
)
_HOTEL_LABEL_RE = re.compile(
    r"(?:Hotel search URL|Hotels?[^\n]{0,40}Search URL)\s*:\s*(https?://\S+)",
    re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    """Remove ```html / ``` markdown fences if the model wrapped the answer."""
    text = text.strip()
    fence = re.match(r"^```(?:html|markdown|md)?\s*\n([\s\S]*?)\n```\s*$", text, re.I)
    if fence:
        return fence.group(1).strip()
    text = re.sub(r"^```(?:html|markdown|md)?\s*\n", "", text, flags=re.I)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def _looks_like_html(text: str) -> bool:
    sample = text.lstrip()[:400].lower()
    return bool(
        re.search(r"<(article|section|h[1-4]|p|ul|ol|table|div)\b", sample)
    )


def _plain_fallback(text: str) -> str:
    escaped = html.escape(text)
    blocks = [b.strip() for b in re.split(r"\n\s*\n", escaped) if b.strip()]
    parts: list[str] = []
    for block in blocks:
        lines = block.split("\n")
        if len(lines) == 1 and re.match(r"^[A-Z0-9][A-Z0-9 \-:/]{2,}$", lines[0]):
            parts.append(f"<h2>{lines[0].title() if lines[0].isupper() else lines[0]}</h2>")
        elif all(re.match(r"^[-*•]\s+", ln) for ln in lines):
            items = "".join(
                f"<li>{re.sub(r'^[-*•]\\s+', '', ln)}</li>" for ln in lines
            )
            parts.append(f"<ul>{items}</ul>")
        else:
            parts.append("<p>" + "<br>".join(lines) + "</p>")
    return "".join(parts) or f"<p>{escaped}</p>"


def _clean_url(url: str) -> str:
    return url.strip().rstrip(").,;\"'>]")


def extract_booking_links(*texts: str) -> dict[str, str | None]:
    """Pull Trip.com flight/hotel result URLs from tool output or plan text."""
    blob = "\n".join(t for t in texts if t)
    links: dict[str, str | None] = {"flight": None, "hotel": None}

    flight_m = _FLIGHT_LABEL_RE.search(blob)
    if flight_m:
        links["flight"] = _clean_url(flight_m.group(1))
    hotel_m = _HOTEL_LABEL_RE.search(blob)
    if hotel_m:
        links["hotel"] = _clean_url(hotel_m.group(1))

    # plan_trip style blocks: "1) FLIGHTS ... Search URL:" then later hotels
    flight_block = re.search(
        r"1\)\s*FLIGHTS[\s\S]{0,400}?Search URL:\s*(https?://\S+)",
        blob,
        re.IGNORECASE,
    )
    if flight_block and not links["flight"]:
        links["flight"] = _clean_url(flight_block.group(1))

    hotel_block = re.search(
        r"2\)\s*HOTELS[\s\S]{0,400}?Search URL:\s*(https?://\S+)",
        blob,
        re.IGNORECASE,
    )
    if hotel_block and not links["hotel"]:
        links["hotel"] = _clean_url(hotel_block.group(1))

    for raw in _URL_RE.findall(blob):
        url = _clean_url(raw)
        lower = url.lower()
        if "trip.com" not in lower and "ctrip.com" not in lower:
            continue
        if links["flight"] is None and "/flight" in lower:
            links["flight"] = url
        elif links["hotel"] is None and "/hotel" in lower:
            links["hotel"] = url

    return links


def _booking_links_section(links: dict[str, str | None]) -> str:
    items: list[str] = []
    if links.get("flight"):
        href = html.escape(links["flight"], quote=True)
        items.append(
            f'<li><a class="booking-link booking-link-flight" href="{href}" '
            f'target="_blank" rel="noopener noreferrer">View flights on Trip.com</a></li>'
        )
    if links.get("hotel"):
        href = html.escape(links["hotel"], quote=True)
        items.append(
            f'<li><a class="booking-link booking-link-hotel" href="{href}" '
            f'target="_blank" rel="noopener noreferrer">View hotels on Trip.com</a></li>'
        )
    if not items:
        return ""
    return (
        '<section class="booking-links">'
        "<h2>Booking links</h2>"
        "<p>Open the live flight and hotel results:</p>"
        f'<ul class="booking-link-list">{"".join(items)}</ul>'
        "</section>"
    )


def _html_has_href(body: str, url: str | None) -> bool:
    if not url:
        return True
    return html.escape(url, quote=True) in body or url in body


def ensure_booking_links(body_html: str, links: dict[str, str | None] | None) -> str:
    """Append a booking-links section when flight/hotel URLs are missing from HTML."""
    if not links or (not links.get("flight") and not links.get("hotel")):
        return body_html

    missing = {
        "flight": links.get("flight") if not _html_has_href(body_html, links.get("flight")) else None,
        "hotel": links.get("hotel") if not _html_has_href(body_html, links.get("hotel")) else None,
    }
    if not missing["flight"] and not missing["hotel"]:
        # URLs are already present; still add a clear CTA block if none exists.
        if 'class="booking-links"' in body_html or "Booking links" in body_html:
            return body_html
        section = _booking_links_section(links)
        return body_html + section if section else body_html

    section = _booking_links_section(
        {
            "flight": missing["flight"] or links.get("flight"),
            "hotel": missing["hotel"] or links.get("hotel"),
        }
    )
    if not section:
        return body_html
    return body_html + section


def sanitize_plan_html(raw_html: str) -> str:
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
        skip_tags=["pre", "code"],
    )
    return cleaned


def plan_to_html(
    plan_text: str,
    booking_links: dict[str, str | None] | None = None,
    extra_text: str = "",
) -> str:
    """Convert model output (HTML or markdown/plain) into sanitized HTML."""
    text = _strip_fences(plan_text or "")
    if not text:
        return "<p>No plan was generated.</p>"

    links = booking_links or extract_booking_links(text, extra_text)
    # Also mine unlabeled URLs from the answer itself.
    mined = extract_booking_links(text, extra_text)
    links = {
        "flight": links.get("flight") or mined.get("flight"),
        "hotel": links.get("hotel") or mined.get("hotel"),
    }

    if _looks_like_html(text):
        body = text
    else:
        try:
            body = markdown.markdown(
                text,
                extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
            )
        except Exception:
            body = _plain_fallback(text)

    body = ensure_booking_links(body, links)
    inner = sanitize_plan_html(body).strip()
    if inner.lower().startswith("<article"):
        inner = re.sub(
            r"^<article(?:\s[^>]*)?>|</article>\s*$",
            "",
            inner,
            flags=re.I,
        ).strip()
    return f'<article class="plan-doc">{inner}</article>'


def tool_messages_text(messages: list[dict[str, Any]]) -> str:
    """Concatenate tool-role message contents for URL mining."""
    chunks: list[str] = []
    for message in messages:
        if message.get("role") == "tool":
            content = message.get("content") or ""
            if content:
                chunks.append(str(content))
    return "\n".join(chunks)
