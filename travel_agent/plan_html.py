"""Turn agent plan text into safe, displayable HTML."""

from __future__ import annotations

import html
import re

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
    "a": ["href", "title", "rel", "target"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "*": ["class"],
}


def _strip_fences(text: str) -> str:
    """Remove ```html / ``` markdown fences if the model wrapped the answer."""
    text = text.strip()
    fence = re.match(r"^```(?:html|markdown|md)?\s*\n([\s\S]*?)\n```\s*$", text, re.I)
    if fence:
        return fence.group(1).strip()
    # Partial fence at start/end
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


def sanitize_plan_html(raw_html: str) -> str:
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    # Force safe link behavior
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank],
        skip_tags=["pre", "code"],
    )
    return cleaned


def plan_to_html(plan_text: str) -> str:
    """Convert model output (HTML or markdown/plain) into sanitized HTML."""
    text = _strip_fences(plan_text or "")
    if not text:
        return "<p>No plan was generated.</p>"

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

    inner = sanitize_plan_html(body)
    return f'<article class="plan-doc">{inner}</article>'
