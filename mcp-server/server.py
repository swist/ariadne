import os
import re
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

import xml.etree.ElementTree as ET

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Car Search",
    instructions=(
        "Search Craigslist for used cars.\n\n"
        "Workflow:\n"
        "1. set_criteria() — define what you want (make, model, price, year, mileage, location)\n"
        "2. search_cars() — find matching listings\n"
        "3. get_listing(url) — full details on a specific car\n"
        "4. save_listing(url, notes) — bookmark interesting ones\n"
        "5. list_saved() — review bookmarks\n\n"
        "You can call set_criteria() multiple times to refine filters mid-conversation."
    ),
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class Criteria:
    makes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    max_mileage: Optional[int] = None
    locations: list[str] = field(default_factory=lambda: ["sfbay"])
    keywords: str = ""


_criteria = Criteria()
_saved: list[dict] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_search_url(city: str, c: Criteria) -> str:
    parts = list(c.makes) + list(c.models)
    if c.keywords:
        parts.append(c.keywords)

    params: list[tuple[str, str | int]] = [("format", "rss"), ("srchType", "A")]
    if parts:
        params.append(("query", " ".join(parts)))
    if c.min_price is not None:
        params.append(("min_price", c.min_price))
    if c.max_price is not None:
        params.append(("max_price", c.max_price))
    if c.min_year is not None:
        params.append(("min_auto_year", c.min_year))
    if c.max_year is not None:
        params.append(("max_auto_year", c.max_year))
    if c.max_mileage is not None:
        params.append(("auto_miles_max", c.max_mileage))

    qs = "&".join(f"{k}={v}" for k, v in params)
    return f"https://{city}.craigslist.org/search/cta?{qs}"


_RSS_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rss": "http://purl.org/rss/1.0/",
    "dc":  "http://purl.org/dc/elements/1.1/",
}


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse a Craigslist RSS 1.0 (RDF) feed into a list of listing dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for item in root.findall("rss:item", _RSS_NS):
        items.append({
            "title":       item.findtext("rss:title",       "", _RSS_NS),
            "link":        item.findtext("rss:link",        "", _RSS_NS),
            "description": item.findtext("rss:description", "", _RSS_NS),
            "published":   item.findtext("dc:date",         "", _RSS_NS),
        })
    return items


def _active(c: Criteria) -> dict:
    return {k: v for k, v in asdict(c).items() if v not in (None, [], "")}


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def set_criteria(
    makes: Optional[list[str]] = None,
    models: Optional[list[str]] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    max_mileage: Optional[int] = None,
    locations: Optional[list[str]] = None,
    keywords: Optional[str] = None,
) -> str:
    """Set or update car search criteria. Only the provided parameters are changed;
    everything else stays as-is. Call this multiple times to refine your search.

    Common Craigslist city slugs:
      sfbay, newyork, losangeles, chicago, seattle, austin, denver, miami,
      boston, portland, sandiego, phoenix, houston, dallas, atlanta, minneapolis,
      washingtondc, philadelphia, detroit, cleveland, columbus, charlotte

    Args:
        makes: Manufacturers, e.g. ["Toyota", "Honda", "Subaru"]
        models: Model names, e.g. ["Camry", "Civic", "Outback"]
        min_year: Earliest model year (e.g. 2018)
        max_year: Latest model year (e.g. 2024)
        min_price: Minimum price in USD
        max_price: Maximum price in USD
        max_mileage: Maximum odometer reading in miles
        locations: List of Craigslist city slugs to search
        keywords: Extra search terms — trim levels, features, color, etc.
    """
    global _criteria
    if makes is not None:
        _criteria.makes = makes
    if models is not None:
        _criteria.models = models
    if min_year is not None:
        _criteria.min_year = min_year
    if max_year is not None:
        _criteria.max_year = max_year
    if min_price is not None:
        _criteria.min_price = min_price
    if max_price is not None:
        _criteria.max_price = max_price
    if max_mileage is not None:
        _criteria.max_mileage = max_mileage
    if locations is not None:
        _criteria.locations = locations
    if keywords is not None:
        _criteria.keywords = keywords

    return f"Criteria saved:\n{json.dumps(_active(_criteria), indent=2)}"


@mcp.tool()
def get_criteria() -> str:
    """Show the current search criteria."""
    active = _active(_criteria)
    if not active:
        return "No criteria set yet. Use set_criteria() to define what you're looking for."
    return json.dumps(active, indent=2)


@mcp.tool()
def reset_criteria() -> str:
    """Clear all search criteria back to defaults (empty filters, location: sfbay)."""
    global _criteria
    _criteria = Criteria()
    return "All criteria cleared."


@mcp.tool()
async def search_cars(
    limit: int = 25,
    location: Optional[str] = None,
) -> str:
    """Search Craigslist for cars matching the saved criteria.

    Set your filters with set_criteria() before calling this.

    Args:
        limit: Max listings to return per location (1–50, default 25)
        location: Override saved location(s) with a single Craigslist city slug
    """
    c = _criteria
    cities = [location] if location else (c.locations or ["sfbay"])
    limit = max(1, min(50, limit))

    blocks: list[str] = []
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        for city in cities:
            url = _build_search_url(city, c)
            try:
                r = await client.get(url)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                blocks.append(f"[{city}] HTTP {exc.response.status_code}")
                continue
            except Exception as exc:
                blocks.append(f"[{city}] Error: {exc}")
                continue

            entries = _parse_rss(r.text)[:limit]

            if not entries:
                blocks.append(f"\n[{city}] No listings found.\nSearch URL: {url}")
                continue

            lines = [
                f"\n{'=' * 55}",
                f"{city.upper()} — {len(entries)} listings",
                f"{'=' * 55}",
            ]
            for i, e in enumerate(entries, 1):
                title = e["title"] or "(no title)"
                link = e["link"]
                pub = e["published"][:16]
                snippet = re.sub(r"<[^>]+>", " ", e["description"])
                snippet = re.sub(r"\s+", " ", snippet).strip()[:180]
                lines.append(f"\n{i}. {title}")
                if pub:
                    lines.append(f"   Posted: {pub}")
                if snippet:
                    lines.append(f"   {snippet}")
                lines.append(f"   {link}")

            blocks.append("\n".join(lines))

    if not blocks:
        return "No results."

    header = f"Active criteria: {json.dumps(_active(c))}\n"
    return header + "\n".join(blocks)


@mcp.tool()
async def get_listing(url: str) -> str:
    """Fetch full details for a single Craigslist car listing.

    Args:
        url: The Craigslist listing URL
    """
    from bs4 import BeautifulSoup

    if "craigslist.org" not in url:
        return "Error: only Craigslist URLs are supported."

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
        except Exception as exc:
            return f"Failed to fetch listing: {exc}"

    # Detect expired/deleted listing
    if "This posting has been deleted" in r.text or "This posting has expired" in r.text:
        return "This listing has been deleted or has expired."

    soup = BeautifulSoup(r.text, "html.parser")

    # Title
    title = ""
    for sel in ["#titletextonly", "span#titletextonly", ".postingtitletext"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break

    # Price
    price_el = soup.select_one(".price")
    price = price_el.get_text(strip=True) if price_el else "not listed"

    # Neighborhood
    hood_el = soup.select_one(".postingtitletext small")
    hood = hood_el.get_text(strip=True).strip("() ") if hood_el else ""

    # Posted date
    date_el = soup.select_one("time.date.timeago") or soup.select_one("p.postinginfo time")
    post_date = (date_el.get("datetime") or date_el.get_text(strip=True)) if date_el else ""

    # Vehicle attributes from attrgroups
    attrs: dict[str, str] = {}
    for group in soup.select(".attrgroup"):
        for span in group.select("span"):
            text = span.get_text(separator=" ", strip=True)
            if ":" in text:
                k, _, v = text.partition(":")
                attrs[k.strip()] = v.strip()
            elif text:
                attrs[text] = "✓"

    # Description
    desc_el = soup.select_one("#postingbody")
    description = ""
    if desc_el:
        for junk in desc_el.select(".print-information, .QR-code-target"):
            junk.decompose()
        description = desc_el.get_text(separator="\n", strip=True)[:3000]

    # Images
    img_urls = [a["href"] for a in soup.select(".gallery a[href]")]

    lines = [f"# {title or '(no title)'}"]
    lines.append(f"Price: {price}")
    if hood:
        lines.append(f"Location: {hood}")
    if post_date:
        lines.append(f"Posted: {post_date[:10]}")
    lines.append(f"URL: {url}")

    if attrs:
        lines.append("\n## Vehicle Details")
        for k, v in attrs.items():
            lines.append(f"  {k}: {v}")

    if description:
        lines.append(f"\n## Description\n{description}")

    if img_urls:
        lines.append(f"\n## Images ({len(img_urls)} total, showing first 8)")
        for img in img_urls[:8]:
            lines.append(f"  {img}")

    return "\n".join(lines)


@mcp.tool()
def save_listing(url: str, notes: str = "") -> str:
    """Bookmark a listing URL for later review, with optional notes.

    Args:
        url: The Craigslist listing URL
        notes: Anything worth remembering (price negotiation ideas, concerns, etc.)
    """
    for item in _saved:
        if item["url"] == url:
            item["notes"] = notes
            return f"Updated notes for already-saved listing."
    _saved.append({"url": url, "notes": notes})
    return f"Saved listing #{len(_saved)}."


@mcp.tool()
def list_saved() -> str:
    """List all bookmarked car listings."""
    if not _saved:
        return "No listings saved yet. Use save_listing(url) to bookmark one."
    lines = [f"Saved listings ({len(_saved)} total):\n"]
    for i, item in enumerate(_saved, 1):
        lines.append(f"{i}. {item['url']}")
        if item.get("notes"):
            lines.append(f"   Notes: {item['notes']}")
    return "\n".join(lines)


@mcp.tool()
def remove_saved(index: int) -> str:
    """Remove a bookmarked listing by its 1-based index from list_saved().

    Args:
        index: Position number shown in list_saved()
    """
    if not 1 <= index <= len(_saved):
        return f"Invalid index {index}. You have {len(_saved)} saved listing(s)."
    removed = _saved.pop(index - 1)
    return f"Removed: {removed['url']}"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=port, log_level="info")
