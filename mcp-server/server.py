"""UK car search MCP server.

Sources: AutoTrader UK, PistonHeads, Car & Classic, The Market, eBay Motors, Gumtree.
Uses curl_cffi (chrome TLS impersonation) to bypass Cloudflare on most sites.
"""

import asyncio, json, os, re
from dataclasses import dataclass, field, asdict
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from mcp.server.fastmcp import FastMCP


VALID_SITES = ["autotrader", "pistonheads", "carandclassic", "themarket", "ebay", "gumtree"]

mcp = FastMCP(
    "UK Car Search",
    instructions=(
        "Search UK car listings: AutoTrader UK, PistonHeads, Car & Classic, "
        "The Market, eBay Motors UK, and Gumtree.\n\n"
        "Typical workflow:\n"
        "1. set_criteria() — tell me what you want (make, model, year, budget, etc.)\n"
        "2. search_cars() — fetch results across all (or selected) sites\n"
        "3. get_listing(url) — full detail on a specific car\n"
        "4. save_listing(url, notes) / list_saved() — bookmark cars\n\n"
        f"Sites: {', '.join(VALID_SITES)}"
    ),
)

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Criteria:
    makes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    min_price: Optional[int] = None     # GBP
    max_price: Optional[int] = None     # GBP
    max_mileage: Optional[int] = None
    postcode: str = "SW1A 1AA"
    radius_miles: int = 50
    fuel_type: Optional[str] = None     # petrol / diesel / electric / hybrid
    transmission: Optional[str] = None  # manual / automatic
    keywords: str = ""
    sites: list[str] = field(default_factory=lambda: list(VALID_SITES))


@dataclass
class Listing:
    title: str
    price: str
    url: str
    source: str
    year: Optional[int] = None
    mileage: Optional[str] = None
    location: Optional[str] = None
    fuel: Optional[str] = None
    transmission: Optional[str] = None
    summary: Optional[str] = None


_criteria = Criteria()
_saved: list[dict] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _active(c: Criteria) -> dict:
    return {k: v for k, v in asdict(c).items() if v not in (None, [], "")}


def _query(c: Criteria) -> str:
    parts = list(c.makes) + list(c.models)
    if c.keywords:
        parts.append(c.keywords)
    return " ".join(parts)


def _next_data(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            pass
    return {}


def _dig(obj, *keys):
    """Safely traverse nested dicts/lists, returning None on any miss."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int) and len(obj) > k:
            obj = obj[k]
        else:
            return None
        if obj is None:
            return None
    return obj


def _gbp(v) -> str:
    if v is None:
        return "?"
    try:
        return f"£{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


def _miles(v) -> Optional[str]:
    if v is None:
        return None
    try:
        return f"{int(v):,} miles"
    except (TypeError, ValueError):
        return str(v)


def _listing_line(i: int, l: Listing) -> str:
    parts = [f"{i}. {l.title}  |  {l.price}"]
    details = [x for x in [
        str(l.year) if l.year else None,
        l.mileage, l.fuel, l.transmission
    ] if x]
    if details:
        parts.append("   " + " · ".join(details))
    if l.location:
        parts.append(f"   {l.location}")
    if l.summary:
        parts.append(f"   {l.summary[:150]}")
    parts.append(f"   {l.url}")
    return "\n".join(parts)


# ── Site search scrapers ───────────────────────────────────────────────────────

def _autotrader_base_params(c: Criteria) -> dict[str, str]:
    """Build the non-make/model AutoTrader query params from criteria."""
    p: dict[str, str] = {
        "postcode": c.postcode.replace(" ", ""),
        "radius": str(c.radius_miles),
        "onesearchad": "Used",
        "sort": "relevance",
    }
    if c.min_year:
        p["year-from"] = str(c.min_year)
    if c.max_year:
        p["year-to"] = str(c.max_year)
    if c.min_price:
        p["price-from"] = str(c.min_price)
    if c.max_price:
        p["price-to"] = str(c.max_price)
    if c.max_mileage:
        p["mileage-to"] = str(c.max_mileage)
    if c.fuel_type:
        p["fuel-type"] = c.fuel_type.capitalize()
    if c.transmission:
        p["transmission"] = c.transmission.capitalize()
    if c.keywords:
        p["keywords"] = c.keywords.replace(" ", "+")
    return p


def _autotrader_parse_raw(raw: list, limit: int) -> list[Listing]:
    out = []
    for item in raw[:limit]:
        p_obj = item.get("price") or {}
        price = (
            (p_obj.get("retailPriceLabel") or p_obj.get("purchasePriceLabel"))
            if isinstance(p_obj, dict) else None
        ) or _gbp((p_obj.get("retailPrice") or p_obj.get("purchasePrice")) if isinstance(p_obj, dict) else p_obj)
        advert_id = item.get("id", "")
        href = f"https://www.autotrader.co.uk/car-details/{advert_id}" if advert_id else ""
        specs = item.get("keyFeatures") or []
        loc = item.get("location") or {}
        location = (loc.get("town") or loc.get("postCode", "")) if isinstance(loc, dict) else str(loc)
        out.append(Listing(
            title=item.get("heading") or f"{item.get('make','')} {item.get('model','')}".strip(),
            price=price,
            url=href,
            source="AutoTrader UK",
            year=item.get("year"),
            mileage=_miles(item.get("mileage")),
            location=location,
            fuel=next((x for x in specs if any(f in x.lower() for f in ["petrol","diesel","electric","hybrid"])), None),
            transmission=next((x for x in specs if any(t in x.lower() for t in ["manual","automatic"])), None),
        ))
    return out


async def _autotrader_one_query(params: dict[str, str], s: AsyncSession, limit: int) -> list[Listing]:
    """Run a single AutoTrader search and return listings."""
    url = "https://www.autotrader.co.uk/car-search?" + "&".join(f"{k}={v}" for k, v in params.items())
    r = await s.get(url, headers=_HEADERS)
    data = _next_data(r.text)
    pp = _dig(data, "props", "pageProps") or {}

    raw = (
        _dig(pp, "initialState", "inventory", "listings") or
        _dig(pp, "searchResults", "advertSummaries") or
        _dig(pp, "advertSummaries") or
        []
    )
    if raw:
        out = _autotrader_parse_raw(raw, limit)
        if out:
            return out

    # HTML fallback
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for article in soup.select("li[data-advert-id], article[data-standout-type]")[:limit]:
        a = article.select_one("h3 a, h2 a")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://www.autotrader.co.uk" + href
        price_el = article.select_one("[class*='price'], [data-testid='price']")
        loc_el = article.select_one("[class*='location']")
        out.append(Listing(
            title=a.get_text(strip=True),
            price=price_el.get_text(strip=True) if price_el else "?",
            url=href,
            source="AutoTrader UK",
            location=loc_el.get_text(strip=True) if loc_el else "",
        ))
    return out


async def _search_autotrader(c: Criteria, s: AsyncSession, limit: int) -> list[Listing]:
    base = _autotrader_base_params(c)

    # AutoTrader only accepts one make per request — fan out if multiple makes given,
    # pairing each make with any corresponding model (or no model if lists differ in length).
    if len(c.makes) <= 1:
        params = dict(base)
        if c.makes:
            params["make"] = c.makes[0].upper()
        if c.models:
            params["model"] = c.models[0].upper()
        return await _autotrader_one_query(params, s, limit)

    # Multiple makes: run one query per make, interleave results up to `limit` total.
    per_make = max(3, limit // len(c.makes))
    tasks = []
    for i, make in enumerate(c.makes):
        params = dict(base)
        params["make"] = make.upper()
        # Pair with same-index model if available
        if i < len(c.models):
            params["model"] = c.models[i].upper()
        tasks.append(_autotrader_one_query(params, s, per_make))

    batches = await asyncio.gather(*tasks, return_exceptions=True)

    # Interleave results round-robin so output isn't make-grouped
    seen: set[str] = set()
    merged: list[Listing] = []
    iters = [iter(b) for b in batches if isinstance(b, list)]
    while len(merged) < limit and iters:
        exhausted = []
        for it in iters:
            try:
                listing = next(it)
                if listing.url not in seen:
                    seen.add(listing.url)
                    merged.append(listing)
            except StopIteration:
                exhausted.append(it)
        for it in exhausted:
            iters.remove(it)
        if not iters:
            break

    return merged[:limit]


async def _search_pistonheads(c: Criteria, s: AsyncSession, limit: int) -> list[Listing]:
    q = _query(c) or "car"
    params = [("q", q.replace(" ", "+"))]
    if c.min_year:
        params.append(("minYear", str(c.min_year)))
    if c.max_year:
        params.append(("maxYear", str(c.max_year)))
    if c.min_price:
        params.append(("minPrice", str(c.min_price)))
    if c.max_price:
        params.append(("maxPrice", str(c.max_price)))
    if c.max_mileage:
        params.append(("maxMileage", str(c.max_mileage)))
    if c.postcode:
        params.append(("location", c.postcode.replace(" ", "+")))
        params.append(("distance", str(c.radius_miles)))

    qs = "&".join(f"{k}={v}" for k, v in params)
    url = f"https://www.pistonheads.com/classifieds?{qs}"
    r = await s.get(url, headers=_HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")

    # MUI components use hashed class names — anchor on href pattern instead
    seen: set[str] = set()
    out = []
    for a in soup.find_all("a", href=re.compile(r"/classifieds/used-cars/[^/]+/[^/]+/\d+")):
        href = a.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        if not href.startswith("http"):
            href = "https://www.pistonheads.com" + href

        card = a.find_parent(["li", "article"]) or a.parent
        card_text = card.get_text(" ", strip=True) if card else a.get_text(strip=True)
        title = a.get_text(strip=True) or card_text[:80]

        price_m = re.search(r"£[\d,]+", card_text)
        miles_m = re.search(r"[\d,]+\s*miles", card_text, re.I)
        year_m = re.search(r"\b(19|20)\d{2}\b", card_text)
        fuel_m = re.search(r"petrol|diesel|electric|hybrid", card_text, re.I)
        trans_m = re.search(r"\bmanual\b|\bautomatic\b", card_text, re.I)

        out.append(Listing(
            title=title,
            price=price_m.group() if price_m else "?",
            url=href,
            source="PistonHeads",
            year=int(year_m.group()) if year_m else None,
            mileage=miles_m.group().strip() if miles_m else None,
            fuel=fuel_m.group().capitalize() if fuel_m else None,
            transmission=trans_m.group().capitalize() if trans_m else None,
        ))
        if len(out) >= limit:
            break
    return out


async def _search_carandclassic(c: Criteria, s: AsyncSession, limit: int) -> list[Listing]:
    params = [("country", "gb"), ("currency", "GBP")]
    q = _query(c)
    if q:
        params.append(("q", q.replace(" ", "+")))
    if c.min_year:
        params.append(("yearFrom", str(c.min_year)))
    if c.max_year:
        params.append(("yearTo", str(c.max_year)))
    if c.min_price:
        params.append(("minPrice", str(c.min_price)))
    if c.max_price:
        params.append(("maxPrice", str(c.max_price)))

    qs = "&".join(f"{k}={v}" for k, v in params)
    url = f"https://www.carandclassic.com/search?{qs}"
    r = await s.get(url, headers=_HEADERS)
    data = _next_data(r.text)
    pp = _dig(data, "props", "pageProps") or {}

    raw = (
        _dig(pp, "listings") or
        _dig(pp, "results") or
        _dig(pp, "cars") or
        _dig(pp, "data", "listings") or
        []
    )

    if raw:
        out = []
        for item in raw[:limit]:
            p = item.get("price") or item.get("listPrice")
            price = _gbp(p) if isinstance(p, (int, float)) else str(p or "?")
            slug = item.get("slug") or item.get("id") or ""
            href = f"https://www.carandclassic.com/car/{slug}" if slug else ""
            out.append(Listing(
                title=item.get("title") or item.get("name", ""),
                price=price,
                url=href,
                source="Car & Classic",
                year=item.get("year"),
                mileage=_miles(item.get("mileage")),
                location=item.get("location") or item.get("town", ""),
            ))
        if out:
            return out

    # HTML fallback
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"/car/|/classic-cars-for-sale/")):
        href = a.get("href", "")
        if href in seen or not href:
            continue
        seen.add(href)
        if not href.startswith("http"):
            href = "https://www.carandclassic.com" + href
        card = a.find_parent(["article", "li"]) or a.parent
        card_text = card.get_text(" ", strip=True) if card else ""
        title = a.get_text(strip=True)
        if not title or len(title) < 4:
            continue
        price_m = re.search(r"£[\d,]+", card_text)
        year_m = re.search(r"\b(19|20)\d{2}\b", card_text)
        out.append(Listing(
            title=title,
            price=price_m.group() if price_m else "?",
            url=href,
            source="Car & Classic",
            year=int(year_m.group()) if year_m else None,
        ))
        if len(out) >= limit:
            break
    return out


async def _search_themarket(c: Criteria, s: AsyncSession, limit: int) -> list[Listing]:
    params: list[tuple[str, str]] = []
    q = _query(c)
    if q:
        params.append(("q", q.replace(" ", "+")))
    if c.min_price:
        params.append(("minPrice", str(c.min_price)))
    if c.max_price:
        params.append(("maxPrice", str(c.max_price)))
    if c.min_year:
        params.append(("yearFrom", str(c.min_year)))
    if c.max_year:
        params.append(("yearTo", str(c.max_year)))

    qs = ("?" + "&".join(f"{k}={v}" for k, v in params)) if params else ""
    url = f"https://www.themarket.com/cars-for-sale{qs}"
    r = await s.get(url, headers=_HEADERS)
    data = _next_data(r.text)
    pp = _dig(data, "props", "pageProps") or {}

    raw = (
        _dig(pp, "listings") or
        _dig(pp, "vehicles") or
        _dig(pp, "cars") or
        _dig(pp, "data", "listings") or
        _dig(pp, "initialData", "listings") or
        []
    )

    if raw:
        out = []
        for item in raw[:limit]:
            if item.get("priceOnRequest") or item.get("hidePrice"):
                price = "POA"
            elif item.get("estimateLow") and item.get("estimateHigh"):
                price = f"Est. {_gbp(item['estimateLow'])}–{_gbp(item['estimateHigh'])}"
            else:
                p = item.get("price") or item.get("askingPrice")
                price = _gbp(p) if isinstance(p, (int, float)) else str(p or "?")
            slug = item.get("slug") or item.get("id") or ""
            href = f"https://www.themarket.com/cars/{slug}" if slug else ""
            out.append(Listing(
                title=item.get("title") or item.get("name", ""),
                price=price,
                url=href,
                source="The Market",
                year=item.get("year"),
                mileage=_miles(item.get("mileage")),
                location=str(item.get("location") or ""),
            ))
        if out:
            return out

    # HTML fallback — try several card patterns
    soup = BeautifulSoup(r.text, "html.parser")
    cards = (
        soup.select("[class*='VehicleCard'],[class*='ListingCard'],[class*='CarCard']") or
        soup.select("article,[data-testid*='car'],[data-testid*='listing']")
    )
    out = []
    for card in cards[:limit]:
        a = card.select_one("a[href]")
        if not a:
            continue
        href = a.get("href", "")
        if not href.startswith("http"):
            href = "https://www.themarket.com" + href
        card_text = card.get_text(" ", strip=True)
        title_el = card.select_one("h2,h3,[class*='title'],[class*='name']")
        price_m = re.search(r"£[\d,]+|POA|Price on application", card_text, re.I)
        year_m = re.search(r"\b(19|20)\d{2}\b", card_text)
        out.append(Listing(
            title=title_el.get_text(strip=True) if title_el else a.get_text(strip=True)[:100],
            price=price_m.group() if price_m else "?",
            url=href,
            source="The Market",
            year=int(year_m.group()) if year_m else None,
        ))
    return out


async def _search_ebay(c: Criteria, s: AsyncSession, limit: int) -> list[Listing]:
    q = _query(c)
    params = [
        ("_sacat", "9801"),
        ("LH_ItemCondition", "3000"),
        ("LH_PrefLoc", "1"),
    ]
    if q:
        params.append(("_nkw", q.replace(" ", "+")))
    if c.min_price:
        params.append(("_udlo", str(c.min_price)))
    if c.max_price:
        params.append(("_udhi", str(c.max_price)))

    qs = "&".join(f"{k}={v}" for k, v in params)
    url = f"https://www.ebay.co.uk/sch/i.html?{qs}"
    r = await s.get(url, headers=_HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")

    out = []
    for card in soup.select("li.s-item"):
        title_el = card.select_one(".s-item__title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if "Shop on eBay" in title:
            continue
        link_el = card.select_one("a.s-item__link")
        href = link_el.get("href", "") if link_el else ""
        price_el = card.select_one(".s-item__price")
        loc_el = card.select_one(".s-item__location,.s-item__itemLocation")
        sub_el = card.select_one(".s-item__subtitle")
        subtitle = sub_el.get_text(" ", strip=True) if sub_el else ""
        year_m = re.search(r"\b(19|20)\d{2}\b", subtitle)
        miles_m = re.search(r"[\d,]+\s*miles", subtitle, re.I)
        fuel_m = re.search(r"petrol|diesel|electric|hybrid", subtitle, re.I)
        trans_m = re.search(r"\bmanual\b|\bautomatic\b", subtitle, re.I)
        out.append(Listing(
            title=title,
            price=price_el.get_text(strip=True) if price_el else "?",
            url=href,
            source="eBay Motors UK",
            year=int(year_m.group()) if year_m else None,
            mileage=miles_m.group().strip() if miles_m else None,
            location=loc_el.get_text(strip=True).replace("Located in: ", "") if loc_el else "",
            fuel=fuel_m.group().capitalize() if fuel_m else None,
            transmission=trans_m.group().capitalize() if trans_m else None,
        ))
        if len(out) >= limit:
            break
    return out


async def _search_gumtree(c: Criteria, s: AsyncSession, limit: int) -> list[Listing]:
    q = _query(c)
    params = [("search_category", "cars-vans-motorbikes")]
    if q:
        params.append(("q", q.replace(" ", "+")))
    if c.min_price:
        params.append(("min_price", str(c.min_price)))
    if c.max_price:
        params.append(("max_price", str(c.max_price)))
    if c.postcode:
        params.append(("search_location", c.postcode.replace(" ", "")))
        params.append(("distance", str(c.radius_miles)))

    qs = "&".join(f"{k}={v}" for k, v in params)
    url = f"https://www.gumtree.com/search?{qs}"
    r = await s.get(url, headers=_HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")

    cards = (
        soup.select("article.listing-maxi,li.listing-maxi") or
        soup.select("[data-q='search-result-anchor']")
    )
    out = []
    for card in cards[:limit]:
        title_el = card.select_one("h2.listing-title a,[class*='listing-title'] a,a[href*='/p/']")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if href.startswith("/"):
            href = "https://www.gumtree.com" + href
        price_el = card.select_one(".listing-price strong,.price,[class*='price']")
        loc_el = card.select_one(".listing-location span,[data-q='listing-location']")
        desc_el = card.select_one(".listing-description,.description,p")
        card_text = card.get_text(" ", strip=True)
        year_m = re.search(r"\b(19|20)\d{2}\b", card_text)
        miles_m = re.search(r"[\d,]+\s*miles", card_text, re.I)
        out.append(Listing(
            title=title_el.get_text(strip=True),
            price=price_el.get_text(strip=True) if price_el else "?",
            url=href,
            source="Gumtree",
            year=int(year_m.group()) if year_m else None,
            mileage=miles_m.group().strip() if miles_m else None,
            location=loc_el.get_text(strip=True) if loc_el else "",
            summary=desc_el.get_text(strip=True)[:200] if desc_el else None,
        ))
    return out


SITE_SCRAPERS: dict[str, object] = {
    "autotrader":   _search_autotrader,
    "pistonheads":  _search_pistonheads,
    "carandclassic": _search_carandclassic,
    "themarket":    _search_themarket,
    "ebay":         _search_ebay,
    "gumtree":      _search_gumtree,
}


# ── Detail scrapers ───────────────────────────────────────────────────────────

def _detail_from_next_data(html: str, url: str) -> Optional[str]:
    data = _next_data(html)
    pp = _dig(data, "props", "pageProps") or {}
    item = (
        pp.get("advert") or pp.get("listing") or
        pp.get("car") or pp.get("vehicle") or {}
    )
    if not item:
        return None

    lines = [f"# {item.get('heading') or item.get('title') or item.get('name') or '(no title)'}"]

    p_obj = item.get("price") or {}
    if isinstance(p_obj, dict):
        price = (
            p_obj.get("retailPriceLabel") or
            p_obj.get("purchasePriceLabel") or
            _gbp(p_obj.get("retailPrice") or p_obj.get("purchasePrice"))
        )
    elif isinstance(p_obj, (int, float)):
        price = _gbp(p_obj)
    else:
        price = str(p_obj or "?")
    lines.append(f"Price: {price}")

    for label, key in [("Year", "year"), ("Mileage", "mileage"), ("Colour", "colour")]:
        v = item.get(key)
        if v:
            lines.append(f"{label}: {_miles(v) if key == 'mileage' else v}")

    loc = item.get("location") or {}
    loc_str = (loc.get("town") or loc.get("postCode", "")) if isinstance(loc, dict) else str(loc)
    if loc_str:
        lines.append(f"Location: {loc_str}")

    lines.append(f"URL: {url}")

    specs = item.get("keyFeatures") or item.get("features") or item.get("specs") or []
    if specs:
        lines.append("\n## Specs")
        for sp in specs:
            lines.append(f"  - {sp}")

    desc = item.get("description") or item.get("body") or ""
    if desc:
        lines.append(f"\n## Description\n{str(desc)[:3000]}")

    imgs = item.get("images") or item.get("photos") or []
    if imgs:
        lines.append(f"\n## Images ({len(imgs)} total, showing first 6)")
        for img in imgs[:6]:
            url_img = img.get("url") or img.get("src") or str(img)
            lines.append(f"  {url_img}")

    return "\n".join(lines)


def _detail_from_jsonld(html: str, url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if item.get("@type") in ("Car", "Vehicle", "Product"):
                    lines = [f"# {item.get('name', '(no title)')}", f"URL: {url}"]
                    offers = item.get("offers") or {}
                    if isinstance(offers, dict):
                        p = offers.get("price") or offers.get("lowPrice")
                        cur = offers.get("priceCurrency", "GBP")
                        if p:
                            lines.append(f"Price: {cur} {int(float(p)):,}" if str(p).replace('.','').isdigit() else f"Price: {p}")
                    for k in ("description","brand","model","vehicleModelDate","mileageFromOdometer",
                              "fuelType","vehicleTransmission","color","vehicleEngine","driveWheelConfiguration"):
                        v = item.get(k)
                        if v:
                            val = v.get("name") if isinstance(v, dict) else v
                            lines.append(f"{k}: {val}")
                    return "\n".join(lines)
        except (json.JSONDecodeError, AttributeError):
            pass
    return None


def _detail_generic_html(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    og_title = (soup.find("meta", property="og:title") or {}).get("content", "")
    og_desc = (soup.find("meta", property="og:description") or {}).get("content", "")
    h1 = soup.select_one("h1")
    title = og_title or (h1.get_text(strip=True) if h1 else "(no title)")
    lines = [f"# {title}", f"URL: {url}"]
    if og_desc:
        lines.append(f"\n{og_desc[:500]}")
    return "\n".join(lines)


async def _fetch_and_detail(url: str) -> str:
    domain = url.split("/")[2] if "://" in url else ""
    async with AsyncSession(impersonate="chrome120") as s:
        try:
            r = await s.get(url, headers=_HEADERS, timeout=25)
        except Exception as exc:
            return f"Error fetching {url}: {exc}"
        html = r.text

    # Next.js sites: try __NEXT_DATA__ first
    if any(d in domain for d in ["autotrader.co.uk", "carandclassic.com", "themarket.com", "pistonheads.com"]):
        result = _detail_from_next_data(html, url)
        if result:
            return result

    # JSON-LD (good for eBay detail pages)
    result = _detail_from_jsonld(html, url)
    if result:
        return result

    return _detail_generic_html(html, url)


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def set_criteria(
    makes: Optional[list[str]] = None,
    models: Optional[list[str]] = None,
    min_year: Optional[int] = None,
    max_year: Optional[int] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    max_mileage: Optional[int] = None,
    postcode: Optional[str] = None,
    radius_miles: Optional[int] = None,
    fuel_type: Optional[str] = None,
    transmission: Optional[str] = None,
    keywords: Optional[str] = None,
    sites: Optional[list[str]] = None,
) -> str:
    """Set or update car search criteria. Only provided parameters are changed.

    Args:
        makes: Manufacturers, e.g. ["BMW", "Porsche", "Jaguar"]
        models: Model names, e.g. ["3 Series", "911", "E-Type"]
        min_year: Earliest model year, e.g. 2015
        max_year: Latest model year, e.g. 2022
        min_price: Minimum asking price in GBP
        max_price: Maximum asking price in GBP
        max_mileage: Maximum mileage
        postcode: UK postcode for location-based search, e.g. "SW1A 1AA"
        radius_miles: Search radius in miles from postcode (default 50)
        fuel_type: One of: petrol, diesel, electric, hybrid
        transmission: One of: manual, automatic
        keywords: Extra search terms — trim level, colour, options, etc.
        sites: Which sites to search (default all). Options: autotrader, pistonheads,
               carandclassic, themarket, ebay, gumtree
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
    if postcode is not None:
        _criteria.postcode = postcode
    if radius_miles is not None:
        _criteria.radius_miles = radius_miles
    if fuel_type is not None:
        _criteria.fuel_type = fuel_type.lower()
    if transmission is not None:
        _criteria.transmission = transmission.lower()
    if keywords is not None:
        _criteria.keywords = keywords
    if sites is not None:
        unknown = [x for x in sites if x not in VALID_SITES]
        if unknown:
            return f"Unknown sites: {unknown}. Valid options: {VALID_SITES}"
        _criteria.sites = sites

    return f"Criteria saved:\n{json.dumps(_active(_criteria), indent=2)}"


@mcp.tool()
def get_criteria() -> str:
    """Show the current search criteria."""
    active = _active(_criteria)
    if not active:
        return "No criteria set. Use set_criteria() to define what you're looking for."
    return json.dumps(active, indent=2)


@mcp.tool()
def reset_criteria() -> str:
    """Clear all search criteria back to defaults."""
    global _criteria
    _criteria = Criteria()
    return "Criteria cleared. Defaults: postcode SW1A 1AA, radius 50 miles, all sites."


@mcp.tool()
async def search_cars(
    sites: Optional[list[str]] = None,
    limit: int = 10,
) -> str:
    """Search UK car listing sites for cars matching the saved criteria.

    Set your filters first with set_criteria(), then call this.

    Args:
        sites: Override which sites to search. Options: autotrader, pistonheads,
               carandclassic, themarket, ebay, gumtree. Defaults to criteria.sites.
        limit: Max results per site (1–25, default 10)
    """
    c = _criteria
    active_sites = sites or c.sites or list(VALID_SITES)
    unknown = [x for x in active_sites if x not in VALID_SITES]
    if unknown:
        return f"Unknown sites: {unknown}. Valid: {VALID_SITES}"
    limit = max(1, min(25, limit))

    async with AsyncSession(impersonate="chrome120") as s:
        tasks = [SITE_SCRAPERS[site](c, s, limit) for site in active_sites]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    blocks: list[str] = [f"Search: {json.dumps(_active(c))}\n"]
    total = 0
    for site, result in zip(active_sites, results):
        if isinstance(result, Exception):
            blocks.append(f"\n{'='*50}\n{site.upper()} — ERROR: {result}")
            continue
        listings: list[Listing] = result
        if not listings:
            blocks.append(f"\n{'='*50}\n{site.upper()} — no results")
            continue
        total += len(listings)
        lines = [f"\n{'='*50}", f"{site.upper()} — {len(listings)} results", "="*50]
        for i, l in enumerate(listings, 1):
            lines.append("\n" + _listing_line(i, l))
        blocks.append("\n".join(lines))

    blocks.append(f"\n\nTotal: {total} listings across {len(active_sites)} sites")
    return "".join(blocks)


@mcp.tool()
async def get_listing(url: str) -> str:
    """Fetch full details for a specific car listing.

    Supports: autotrader.co.uk, pistonheads.com, carandclassic.com,
              themarket.com, ebay.co.uk, gumtree.com

    Args:
        url: The listing URL from search results
    """
    supported = ["autotrader.co.uk", "pistonheads.com", "carandclassic.com",
                 "themarket.com", "ebay.co.uk", "gumtree.com"]
    if not any(d in url for d in supported):
        return f"Unsupported URL. Supported domains: {', '.join(supported)}"
    return await _fetch_and_detail(url)


@mcp.tool()
def save_listing(url: str, notes: str = "") -> str:
    """Bookmark a listing URL with optional notes.

    Args:
        url: Listing URL to save
        notes: Notes to yourself — asking price target, concerns, questions, etc.
    """
    for item in _saved:
        if item["url"] == url:
            item["notes"] = notes
            return "Updated notes on already-saved listing."
    _saved.append({"url": url, "notes": notes})
    return f"Saved listing #{len(_saved)}."


@mcp.tool()
def list_saved() -> str:
    """List all bookmarked car listings."""
    if not _saved:
        return "Nothing saved yet. Use save_listing(url) to bookmark a car."
    lines = [f"Saved listings ({len(_saved)}):\n"]
    for i, item in enumerate(_saved, 1):
        lines.append(f"{i}. {item['url']}")
        if item.get("notes"):
            lines.append(f"   Notes: {item['notes']}")
    return "\n".join(lines)


@mcp.tool()
def remove_saved(index: int) -> str:
    """Remove a bookmarked listing by its 1-based index from list_saved().

    Args:
        index: Position shown in list_saved()
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
