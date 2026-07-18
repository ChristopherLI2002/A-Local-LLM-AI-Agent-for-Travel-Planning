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
_CAR_LABEL_RE = re.compile(
    r"(?:Car rental search URL|Cars?[^\n]{0,40}Search URL|Car hire search URL)\s*:\s*(https?://\S+)",
    re.IGNORECASE,
)
_TRAIN_LABEL_RE = re.compile(
    r"(?:Train search URL(?:\s*\([^)]*\))?|Trains?[^\n]{0,40}Search URL)\s*:\s*(https?://\S+)",
    re.IGNORECASE,
)
_TRANSFER_LABEL_RE = re.compile(
    r"(?:Airport transfer search URL|Transfer search URL)\s*:\s*(https?://\S+)",
    re.IGNORECASE,
)

_LINK_KEYS = ("flight", "train", "transfer", "hotel", "car")
_LINK_LABELS = {
    "flight": "flights",
    "train": "trains",
    "transfer": "airport transfers",
    "hotel": "hotels",
    "car": "car rentals",
}


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
    """Pull Trip.com transport/hotel/car result URLs from tool output or plan text."""
    blob = "\n".join(t for t in texts if t)
    links: dict[str, str | None] = {key: None for key in _LINK_KEYS}

    labeled = (
        (_FLIGHT_LABEL_RE, "flight"),
        (_TRAIN_LABEL_RE, "train"),
        (_TRANSFER_LABEL_RE, "transfer"),
        (_HOTEL_LABEL_RE, "hotel"),
        (_CAR_LABEL_RE, "car"),
    )
    for pattern, key in labeled:
        match = pattern.search(blob)
        if match:
            links[key] = _clean_url(match.group(1))

    block_patterns = (
        (r"\)\s*FLIGHTS[\s\S]{0,500}?Search URL:\s*(https?://\S+)", "flight"),
        (r"\)\s*TRAINS[\s\S]{0,500}?Search URL[^\n]*:\s*(https?://\S+)", "train"),
        (
            r"\)\s*AIRPORT TRANSFERS[\s\S]{0,500}?Search URL:\s*(https?://\S+)",
            "transfer",
        ),
        (r"\)\s*HOTELS[\s\S]{0,500}?Search URL:\s*(https?://\S+)", "hotel"),
        (r"\)\s*CAR RENTAL[\s\S]{0,500}?Search URL:\s*(https?://\S+)", "car"),
    )
    for pattern, key in block_patterns:
        match = re.search(pattern, blob, re.IGNORECASE)
        if match and not links[key]:
            links[key] = _clean_url(match.group(1))

    for raw in _URL_RE.findall(blob):
        url = _clean_url(raw)
        lower = url.lower()
        if "trip.com" not in lower and "ctrip.com" not in lower:
            continue
        if links["flight"] is None and "/flight" in lower:
            links["flight"] = url
        elif links["train"] is None and "/train" in lower:
            links["train"] = url
        elif links["transfer"] is None and "airport-transfer" in lower:
            links["transfer"] = url
        elif links["hotel"] is None and "/hotel" in lower:
            links["hotel"] = url
        elif links["car"] is None and "carhire" in lower:
            links["car"] = url

    return links


def _booking_links_section(links: dict[str, str | None]) -> str:
    items: list[str] = []
    button_copy = {
        "flight": "View flights on Trip.com",
        "train": "View trains on Trip.com",
        "transfer": "View airport transfers on Trip.com",
        "hotel": "View hotels on Trip.com",
        "car": "View car rentals on Trip.com",
    }
    for key in _LINK_KEYS:
        url = links.get(key)
        if not url:
            continue
        href = html.escape(url, quote=True)
        items.append(
            f'<li><a class="booking-link booking-link-{key}" href="{href}" '
            f'target="_blank" rel="noopener noreferrer">{button_copy[key]}</a></li>'
        )
    if not items:
        return ""

    kinds = [_LINK_LABELS[k] for k in _LINK_KEYS if links.get(k)]
    if len(kinds) == 1:
        blurb = f"Open the live {kinds[0]} results:"
    elif len(kinds) == 2:
        blurb = f"Open the live {kinds[0]} and {kinds[1]} results:"
    else:
        blurb = "Open the live Trip.com booking results:"

    return (
        '<section class="booking-links">'
        "<h2>Booking links</h2>"
        f"<p>{blurb}</p>"
        f'<ul class="booking-link-list">{"".join(items)}</ul>'
        "</section>"
    )


def _html_has_href(body: str, url: str | None) -> bool:
    if not url:
        return True
    return html.escape(url, quote=True) in body or url in body


def _has_any_link(links: dict[str, str | None] | None) -> bool:
    return bool(links and any(links.get(k) for k in _LINK_KEYS))


def ensure_booking_links(body_html: str, links: dict[str, str | None] | None) -> str:
    """Append a booking-links section when transport/hotel URLs are missing from HTML."""
    if not _has_any_link(links):
        return body_html
    assert links is not None

    missing = {
        key: (links.get(key) if not _html_has_href(body_html, links.get(key)) else None)
        for key in _LINK_KEYS
    }
    if not any(missing.values()):
        if 'class="booking-links"' in body_html or "Booking links" in body_html:
            return body_html
        section = _booking_links_section(links)
        return body_html + section if section else body_html

    section = _booking_links_section(
        {key: missing.get(key) or links.get(key) for key in _LINK_KEYS}
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
    mined = extract_booking_links(text, extra_text)
    links = {key: links.get(key) or mined.get(key) for key in _LINK_KEYS}

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
