"""Fetch Apple Music artist/playlist/album metadata via public OG tags.

No authentication required. Uses the same page a browser would load.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.IGNORECASE)
OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"', re.IGNORECASE)
OG_DESC_RE = re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', re.IGNORECASE)
KIND_RE = re.compile(r"music\.apple\.com/[a-z]{2}/(artist|playlist|album|song)/", re.IGNORECASE)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"


@dataclass
class AppleMetadata:
    url: str
    kind: str  # artist | playlist | album | song | unknown
    title: str | None
    image: str | None
    description: str | None


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> AppleMetadata:
    kind_match = KIND_RE.search(url)
    kind = kind_match.group(1).lower() if kind_match else "unknown"
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": UA}, follow_redirects=True)
    try:
        resp = await client.get(url)
        html = resp.text if resp.status_code == 200 else ""
    except Exception:
        html = ""
    finally:
        if owns_client and client is not None:
            await client.aclose()

    def first(pat: re.Pattern[str]) -> str | None:
        m = pat.search(html)
        return m.group(1).strip() if m else None

    title = first(OG_TITLE_RE)
    if title:
        # Apple's OG title is always "<Name> on Apple Music" (or localized equivalent).
        # Strip it so folder lookups and row labels match the real name.
        title = re.sub(r"\s+on\s+Apple\s+Music\s*$", "", title, flags=re.IGNORECASE).strip()
    # Apple embeds artwork url with {w}x{h} placeholders; we substitute a reasonable size.
    image_raw = first(OG_IMAGE_RE)
    image = (
        image_raw.replace("{w}", "400").replace("{h}", "400").replace("{f}", "jpg").replace("{c}", "cc")
        if image_raw
        else None
    )
    description = first(OG_DESC_RE)
    return AppleMetadata(url=url, kind=kind, title=title, image=image, description=description)
