"""Tools the model can call: web_search (LiteLLM MCP), read_page (fetch + extract text), weather (Open-Meteo).

read_page is reachable by anyone on the mesh (they can ask the bot to read a URL), so it only fetches public
internet addresses: every hostname is resolved through a resolver that refuses private, loopback, link-local and
other non-global IPs (also on every redirect hop, and the connection uses that same vetted resolution).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

log = logging.getLogger("meshbot.tools")

UA = "Mozilla/5.0 (compatible; meshbot/1.0; +text-only mesh radio assistant)"
MAX_BYTES = 8_000_000  # site builders (Wix etc.) ship multi-MB pages


class ToolError(RuntimeError):
    pass


# ---------- read_page: SSRF-safe fetch ----------
def _ip_ok(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


class PublicOnlyResolver(AbstractResolver):
    """Resolve normally, then refuse if any address isn't a public internet address."""

    def __init__(self):
        self._r = ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await self._r.resolve(host, port, family)
        bad = [i["host"] for i in infos if not _ip_ok(i["host"])]
        if bad or not infos:
            raise OSError(f"refusing non-public address for {host}")
        return infos

    async def close(self):
        await self._r.close()


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button", "iframe", "template",
            "aside", "select"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table",
             "ul", "ol", "dd", "dt", "blockquote", "pre", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    text = "".join(p.out)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip(" |") for ln in text.split("\n")]
    lines = [ln for ln in lines if len(ln) > 1]
    return re.sub(r"\s+", " ", p.title).strip(), "\n".join(lines)


def focus(text: str, look_for: str, limit: int) -> str:
    """Too long: keep the start plus the lines that mention what the model is looking for, in page order."""
    if len(text) <= limit:
        return text
    words = [w for w in re.findall(r"\w{3,}", (look_for or "").lower())]
    if not words:
        return text[:limit] + " ..."
    lines = text.split("\n")
    head, used, keep = [], 0, set()
    for i, ln in enumerate(lines):  # the opening ~20% of the budget
        if used > limit // 5:
            break
        head.append(i)
        used += len(ln) + 1
    scored = sorted(((sum(ln.lower().count(w) for w in words), i) for i, ln in enumerate(lines)), reverse=True)
    for score, i in scored:
        if score == 0 or used > limit:
            break
        for j in (i - 1, i, i + 1):  # a little context around each hit
            if 0 <= j < len(lines) and j not in keep and j not in head:
                keep.add(j)
                used += len(lines[j]) + 1
    if not keep:  # nothing matched: plain truncation beats a tiny head
        return text[:limit] + " ..."
    idx = sorted(set(head) | keep)
    out, prev = [], -1
    for i in idx:
        if prev >= 0 and i != prev + 1:
            out.append("...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)[:limit]


async def fetch_page(url: str, look_for: str = "", max_chars: int = 5000, timeout: float = 20) -> tuple[str, dict]:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    conn = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=4, ttl_dns_cache=0)
    tmo = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(connector=conn, timeout=tmo, trust_env=False,
                                     headers={"User-Agent": UA, "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5",
                                              "Accept-Language": "en-US,en;q=0.8"}) as s:
        for _hop in range(5):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ToolError("only http(s) URLs can be read")
            if parts.port not in (None, 80, 443):
                raise ToolError("only standard web ports (80/443) can be read")
            host = parts.hostname
            try:  # literal IPs never reach the resolver
                ipaddress.ip_address(host)
                if not _ip_ok(host):
                    raise ToolError("that address is not on the public internet")
            except ValueError:
                if host.lower() in ("localhost",) or host.lower().endswith((".local", ".lan", ".internal", ".home")):
                    raise ToolError("that address is not on the public internet")
            try:
                async with s.get(url, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        url = urljoin(url, r.headers["Location"])
                        continue
                    if r.status >= 400:
                        raise ToolError(f"site answered HTTP {r.status}")
                    ctype = r.headers.get("Content-Type", "").lower()
                    if not any(t in ctype for t in ("text/", "json", "xml")) and ctype:
                        raise ToolError(f"not a text page ({ctype.split(';')[0]})")
                    chunks, total = [], 0
                    async for chunk in r.content.iter_chunked(65536):  # read() alone returns just the first chunk
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= MAX_BYTES:
                            break
                    raw = b"".join(chunks)
                    try:
                        body = raw.decode(r.charset or "utf-8", "replace")
                    except LookupError:
                        body = raw.decode("utf-8", "replace")
            except aiohttp.ClientConnectorError as e:
                if "non-public" in str(e):
                    raise ToolError("that address is not on the public internet")
                raise ToolError(f"couldn't connect: {e.os_error or e}")
            except OSError as e:
                raise ToolError("that address is not on the public internet" if "non-public" in str(e) else str(e))
            if "html" in ctype or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                title, text = html_to_text(body)
            else:
                title, text = "", body
            text = focus(text, look_for, max_chars)
            if len(text) < 200:
                text += "\n[Very little readable text: the page probably needs JavaScript. Try another result.]"
            head = f"{title}\n{url}\n\n" if title else f"{url}\n\n"
            return head + text, {"url": url, "chars": len(text)}
        raise ToolError("too many redirects")


# ---------- weather: Open-Meteo (+ NWS alerts in the US) ----------
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "heavy showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms w/ hail", 99: "severe thunderstorms w/ hail"}
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
          "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
          "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
          "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
          "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
          "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
          "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
          "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
          "WI": "Wisconsin", "WY": "Wyoming"}


def _compass(deg) -> str:
    if deg is None:
        return ""
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]


async def geocode(client, place: str) -> dict:
    place = place.strip()
    name, _, region = place.partition(",")
    region = region.strip()
    params = {"name": name.strip(), "count": 10, "language": "en", "format": "json"}
    if re.fullmatch(r"\d{5}", name.strip()):
        params["countryCode"] = "US"
    r = await client.get("https://geocoding-api.open-meteo.com/v1/search", params=params, timeout=15)
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        raise ToolError(f"couldn't find a place called {place!r}")
    if region:
        want = STATES.get(region.upper(), region).lower()
        match = [x for x in res if want in (x.get("admin1", "") + " " + x.get("country", "")).lower()
                 or region.upper() == x.get("country_code")]
        res = match or res
    return res[0]


async def weather(client, place: str, units: str = "imperial") -> tuple[str, dict]:
    g = await geocode(client, place)
    imp = units != "metric"
    params = {
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
                   "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max",
    }
    if imp:
        params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
    r = await client.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15)
    r.raise_for_status()
    w = r.json()
    t, sp, pr = ("F", "mph", "in") if imp else ("C", "km/h", "mm")
    c, dl = w.get("current", {}), w.get("daily", {})
    where = ", ".join(x for x in (g.get("name"), g.get("admin1"), g.get("country_code")) if x)
    lines = [f"Weather for {where} (Open-Meteo), local time {c.get('time', '?').replace('T', ' ')}",
             f"Now: {round(c.get('temperature_2m', 0))}{t} (feels {round(c.get('apparent_temperature', 0))}{t}), "
             f"{WMO.get(c.get('weather_code'), 'unknown')}, humidity {c.get('relative_humidity_2m')}%, "
             f"wind {_compass(c.get('wind_direction_10m'))} {round(c.get('wind_speed_10m', 0))} {sp} "
             f"gusting {round(c.get('wind_gusts_10m', 0))}"]
    for i, day in enumerate(dl.get("time") or []):
        lines.append(f"{day}: {WMO.get(dl['weather_code'][i], '?')}, high {round(dl['temperature_2m_max'][i])}{t} "
                     f"low {round(dl['temperature_2m_min'][i])}{t}, precip chance "
                     f"{dl['precipitation_probability_max'][i]}% ({dl['precipitation_sum'][i]} {pr}), "
                     f"max wind {round(dl['wind_speed_10m_max'][i])} {sp}")
    if g.get("country_code") == "US":  # NWS alerts are the authoritative US warnings
        try:
            a = await client.get("https://api.weather.gov/alerts/active",
                                 params={"point": f"{g['latitude']:.4f},{g['longitude']:.4f}"},
                                 headers={"User-Agent": "meshbot (homelab mesh radio assistant)",
                                          "Accept": "application/geo+json"}, timeout=10)
            feats = a.json().get("features") or [] if a.status_code == 200 else []
            if feats:
                lines.append("ACTIVE NWS ALERTS: " + "; ".join(
                    f"{f['properties'].get('event')} until {(f['properties'].get('ends') or f['properties'].get('expires') or '?')[:16]}"
                    for f in feats[:3]))
            else:
                lines.append("No active NWS alerts.")
        except Exception as e:
            log.debug("NWS alerts failed: %s", e)
    return "\n".join(lines), {"place": where}


# ---------- registry ----------
DEFS = {
    "web_search": {
        "description": "Search the web. Returns titles, short snippets and URLs. Snippets are often incomplete: "
                       "follow up with read_page on the best result to get the actual facts.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query, a few keywords"}}, "required": ["query"]}},
    "read_page": {
        "description": "Fetch a web page (e.g. a URL from web_search) and return its readable text.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Full http(s) URL"},
            "look_for": {"type": "string", "description": "What you need from the page, a few keywords "
                                                          "(used to pick the relevant parts of long pages)"}},
            "required": ["url"]}},
    "weather": {
        "description": "Current conditions, 3-day forecast and active NWS alerts for a place. Use this for any "
                       "weather question instead of searching.",
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": "City, 'City, ST', or US ZIP code. Omit for the default "
                                                          "location."}}, "required": []}},
    "callsign_lookup": {
        "description": "Look up an amateur radio callsign: the licensee's first name, last initial and city/state "
                       "(or the club name for club stations). Nothing else is returned.",
        "parameters": {"type": "object", "properties": {
            "callsign": {"type": "string", "description": "The callsign, e.g. W1AW"}}, "required": ["callsign"]}},
    "mesh_node": {
        "description": "Look up one MeshCore node (repeater, companion, room) on the local mesh map: role, when it "
                       "was last heard, distance/direction from home, relay activity, battery, neighbors.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Node name or part of it, or a hex key prefix"}},
            "required": ["name"]}},
    "mesh_nodes": {
        "description": "List/count mesh nodes with filters, e.g. repeaters heard in the last hour, newest nodes, "
                       "busiest relays, nearest nodes.",
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "enum": ["repeater", "companion", "room", "sensor", "observer"]},
            "heard_within_h": {"type": "number", "description": "Only nodes heard within this many hours"},
            "first_seen_within_h": {"type": "number", "description": "Only nodes first seen (new to the map) "
                                                                    "within this many hours"},
            "sort": {"type": "string", "enum": ["recent", "new", "busiest", "nearest"]},
            "limit": {"type": "integer", "description": "Max nodes to list (1-10)"}}, "required": []}},
    "mesh_overview": {
        "description": "Overall state of the local mesh: node counts, packet rates, how many nodes and repeaters "
                       "were heard recently, observers online, busiest relays.",
        "parameters": {"type": "object", "properties": {}, "required": []}},
    "mesh_hops": {
        "description": "How many hops away a node is from this bot, and through which repeaters. A sender's own "
                       "hop count is already shown in the thread; don't call this for that.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Node name or part of it"},
            "live": {"type": "boolean", "description": "Run a live path discovery over the radio (slower, uses "
                                                       "airtime). Only when asked for a fresh/live check or no "
                                                       "route is known."}},
            "required": ["name"]}},
    "repeater_status": {
        "description": "Status of a repeater: with no name, our own home repeater (uptime, traffic, noise floor, "
                       "airtime). With a name, logs in to that repeater as guest over the radio for its status.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Repeater name; omit for our home repeater"}},
            "required": []}},
    "repeater_neighbors": {
        "description": "Neighbors a repeater hears directly, with SNR: with no name, our own home repeater. With a "
                       "name, logs in to that repeater as guest over the radio and asks for its neighbor list.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Repeater name; omit for our home repeater"}},
            "required": []}},
}

MESH_TOOLS = ("mesh_node", "mesh_nodes", "mesh_overview", "mesh_hops", "repeater_status", "repeater_neighbors")


def enabled_tools(channel: dict) -> list[str]:
    names = []
    if channel.get("web_search"):
        names += ["web_search", "read_page"]
    if channel.get("weather"):
        names.append("weather")
    if channel.get("callsign_lookup"):
        names.append("callsign_lookup")
    if channel.get("mesh_data"):
        names += list(MESH_TOOLS)
    return names


def tool_defs(names: list[str]) -> list[dict]:
    return [{"type": "function", "function": {"name": n, **DEFS[n]}} for n in names]


def summarize_args(name: str, args: dict) -> str:
    if name.startswith("ha_"):
        return (name[3:] + " " + json.dumps(args, separators=(",", ":")))[:200]
    if name in MESH_TOOLS:
        bits = [str(args[k]) for k in ("name", "role", "heard_within_h", "first_seen_within_h", "sort") if args.get(k) not in (None, "")]
        if args.get("live"):
            bits.append("live")
        return " ".join(bits)[:200]
    return str(args.get("query") or args.get("url") or args.get("location") or args.get("callsign") or "")[:200]


def parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}
