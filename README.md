# ariadne — UK car search MCP server

An MCP server deployable to [Railway](https://railway.app) that lets you search UK car listings through a Claude conversation. Describe what you want in plain English; Claude translates it into structured searches across six sites simultaneously.

## Sites

| Site | Method |
|------|--------|
| AutoTrader UK | `__NEXT_DATA__` (Next.js) — fans out one request per make |
| PistonHeads | Structural href-pattern scraping |
| Car & Classic | `__NEXT_DATA__` (Next.js) |
| The Market | `__NEXT_DATA__` (Next.js) |
| eBay Motors UK | Stable `.s-item` selectors |
| Gumtree | `article.listing-maxi` / `[data-q]` attrs |

All sites use [curl_cffi](https://github.com/yifeikong/curl-cffi) with Chrome TLS impersonation to bypass Cloudflare.

## Tools Claude gets

| Tool | What it does |
|------|-------------|
| `set_criteria` | Set make, model, year, price (£), mileage, postcode, radius, fuel, gearbox, sites |
| `get_criteria` | Show current filters |
| `reset_criteria` | Clear back to defaults |
| `search_cars` | Search all (or selected) sites in parallel |
| `get_listing` | Full detail — specs, description, images — for one URL |
| `save_listing` | Bookmark a listing with notes |
| `list_saved` | Review bookmarks |
| `remove_saved` | Remove a bookmark by index |

## Deploy to Railway

1. Fork / clone this repo
2. [Create a new Railway project](https://railway.app/new) → **Deploy from GitHub repo** → select this repo
3. Railway picks up `railway.json` at the root, which points to `mcp-server/Dockerfile`
4. Once deployed: **Settings → Networking → Generate Domain**

No environment variables required.

## Connect to Claude

**Claude.ai** — Settings → Integrations → Add custom MCP server:
```
https://<your-app>.up.railway.app/sse
```

**Claude Code CLI** — add to `~/.claude.json`:
```json
{
  "mcpServers": {
    "car-search": {
      "url": "https://<your-app>.up.railway.app/sse"
    }
  }
}
```

## Example conversation

> *"I have £10k, I want a fast luxurious diesel automatic to drive to the south of France — something I can sell easily after."*

Claude will interpret that as BMW 5 Series / Mercedes E-Class / Audi A6 / Jaguar XF, call `set_criteria`, then `search_cars`, and present results from all six sites. You can refine mid-conversation: *"drop Jaguar, add Volvo V90, widen the radius to 100 miles"* and it will update just those fields and re-search.

## Local development

```bash
cd mcp-server
pip install -r requirements.txt
python server.py          # listens on localhost:8000
```

Then point Claude Code at `http://localhost:8000/sse`.
