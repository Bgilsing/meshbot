"""Mesh tools: real-time facts about the MeshCore network for the model.

Passive (no transmissions):
  - CoreScope (MeshCore network analyzer, open REST API): nodes, last heard, neighbors, network stats.
  - openHop REST (our own repeater; needs an API key): a fixed allowlist of read-only GET endpoints only, because
    openHop's tokens are full-admin.
  - The companion's own tables: known contact routes and advert paths.
Active (transmit over the radio, off unless the channel has radio_queries on, rate-limited and cached):
  - guest login to a repeater, then its neighbour list or status; live path discovery.

The model only ever supplies node names and filters, never URLs.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from . import tools as T

log = logging.getLogger("meshbot.meshdata")

OPENHOP_GET = {"stats", "noise_floor_stats", "packet_stats", "crc_error_count", "neighbor_links"}
ROLES = ("repeater", "room", "companion", "sensor", "observer")


# ---------- formatting ----------
def _ts(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ago(v, now: float | None = None) -> str:
    t = _ts(v)
    if t is None:
        return "never"
    s = max(0, (now or time.time()) - t)
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{round(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago".replace(".0h", "h")
    return f"{round(s / 86400)}d ago"


def dist_bearing(home: tuple[float, float] | None, lat, lon, units: str = "imperial") -> str:
    if not home or lat in (None, 0) or lon in (None, 0):
        return ""
    la1, lo1, la2, lo2 = map(math.radians, (home[0], home[1], float(lat), float(lon)))
    d = 2 * 6371 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2
                                       + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    dist = f"{d * 0.621371:.1f}mi" if units == "imperial" else f"{d:.1f}km"
    return f"{dist} {T._compass(brg)}"


def _short(pk: str | None) -> str:
    return (pk or "")[:8]


class MeshTools:
    def __init__(self, cfg, mesh=None, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.mesh = mesh
        self.client = client or httpx.AsyncClient()
        self._cache: dict[str, tuple[float, object]] = {}
        self._home: tuple[str, tuple[float, float] | None] | None = None
        self.radio_log: deque = deque(maxlen=200)    # (time, tool, node, outcome) for every transmission
        self._radio_cache: dict[str, tuple[float, str, dict]] = {}
        self._radio_lock = asyncio.Lock()
        self.last_error: str | None = None

    async def close(self):
        await self.client.aclose()

    # ---------- settings ----------
    def _m(self, key: str):
        return self.cfg.value(f"mesh.{key}")

    def radio_used_last_hour(self) -> int:
        now = time.time()
        return sum(1 for r in self.radio_log if now - r[0] < 3600)

    # ---------- HTTP ----------
    async def _get(self, url: str, params: dict | None = None, headers: dict | None = None, ttl: float | None = None):
        ttl = float(self._m("cache_s")) if ttl is None else ttl
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            r = await self.client.get(url, params=params, headers=headers or {}, timeout=10)
        except httpx.HTTPError as e:
            self.last_error = f"{url.split('/api/')[0]}: {type(e).__name__}"
            raise T.ToolError(f"mesh data source unreachable ({type(e).__name__})")
        if r.status_code == 401:
            self.last_error = "openHop refused the API key"
            raise T.ToolError("openHop refused the API key")
        if r.status_code >= 400:
            self.last_error = f"HTTP {r.status_code} from {url}"
            raise T.ToolError(f"mesh data source returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise T.ToolError("mesh data source returned non-JSON")
        self.last_error = None
        self._cache[key] = (time.time(), data)
        if len(self._cache) > 300:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                self._cache.pop(k, None)
        return data

    async def cs(self, path: str, params: dict | None = None, ttl: float | None = None):
        return await self._get(str(self._m("corescope_url")).rstrip("/") + path, params, ttl=ttl)

    async def oh(self, endpoint: str, params: dict | None = None):
        if endpoint not in OPENHOP_GET:
            raise T.ToolError(f"openHop endpoint {endpoint} is not allowed")
        key = self.cfg.openhop_api_key
        if not key:
            raise T.ToolError("no openHop API key is set (Settings > Secrets), so the home repeater's own stats "
                              "are unavailable")
        data = await self._get(str(self._m("openhop_url")).rstrip("/") + "/api/" + endpoint, params,
                               headers={"X-API-Key": key})
        if isinstance(data, dict) and data.get("success") is False:
            raise T.ToolError(f"openHop: {str(data.get('error'))[:150]}")
        return data.get("data", data) if isinstance(data, dict) else data

    async def home(self) -> tuple[float, float] | None:
        place = str(self.cfg.value("tools.weather_location") or "").strip()
        if self._home and self._home[0] == place:
            return self._home[1]
        loc = None
        if place:
            try:
                g = await T.geocode(self.client, place)
                loc = (float(g["latitude"]), float(g["longitude"]))
            except Exception as e:
                log.debug("home geocode failed: %s", e)
        self._home = (place, loc)
        return loc

    def _units(self) -> str:
        return str(self.cfg.value("tools.weather_units") or "imperial")

    # ---------- node lookup ----------
    async def nodes(self) -> list[dict]:
        d = await self.cs("/api/nodes", {"limit": 2000})
        return d.get("nodes") or [] if isinstance(d, dict) else []

    async def resolve(self, name: str) -> tuple[dict | None, list[dict]]:
        """Best match for a node name (or hex key prefix) and up to 4 other candidates."""
        q = (name or "").strip()
        if not q:
            raise T.ToolError("no node name given")
        nodes = await self.nodes()
        ql = q.lower()
        hexq = ql.replace(" ", "")

        def score(n):
            nm = str(n.get("name") or "").lower()
            pk = str(n.get("public_key") or "").lower()
            if nm == ql:
                s = 0
            elif len(hexq) >= 4 and all(c in "0123456789abcdef" for c in hexq) and pk.startswith(hexq):
                s = 1
            elif nm.startswith(ql):
                s = 2
            elif ql in nm:
                s = 3
            elif all(w in nm for w in ql.split()):
                s = 4
            else:
                return None
            return (s, -(_ts(n.get("last_heard") or n.get("last_seen")) or 0))

        ranked = sorted((sc, i) for i, n in enumerate(nodes) if (sc := score(n)) is not None)
        hits = [nodes[i] for _, i in ranked]
        if not hits:
            try:
                d = await self.cs("/api/nodes/search", {"q": q})
                hits = d.get("nodes") or []
            except T.ToolError:
                hits = []
        return (hits[0] if hits else None), hits[1:5]

    def _names_by_prefix(self, nodes: list[dict], prefix: str) -> str:
        p = prefix.lower()
        m = [n for n in nodes if str(n.get("public_key", "")).lower().startswith(p)]
        if len(m) == 1:
            return str(m[0].get("name") or p)
        if len(m) > 1:
            m.sort(key=lambda n: -(_ts(n.get("last_heard")) or 0))
            return f"{m[0].get('name')}?"  # ambiguous short hash: most recently heard guess
        return p.upper()

    def _node_line(self, n: dict, home, now=None) -> str:
        bits = [f"{n.get('name')} [{n.get('role') or '?'}, key {_short(n.get('public_key'))}]",
                f"heard {ago(n.get('last_heard') or n.get('last_seen'), now)}"]
        db = dist_bearing(home, n.get("lat"), n.get("lon"), self._units())
        if db:
            bits.append(db + " of home")
        return ", ".join(bits)

    # ---------- passive tools ----------
    async def node(self, name: str) -> tuple[str, dict]:
        best, others = await self.resolve(name)
        if not best:
            return f"No node matching '{name}' is known to the network map.", {"found": 0}
        home = await self.home()
        pk = best["public_key"]
        try:
            detail = (await self.cs(f"/api/nodes/{pk}")).get("node") or best
        except T.ToolError:
            detail = best
        n = {**best, **detail}
        lines = [self._node_line(n, home)]
        extra = [f"first seen {ago(n.get('first_seen'))}"]
        if n.get("relay_count_1h") is not None:
            extra.append(f"relayed {n.get('relay_count_1h')} pkts last 1h, {n.get('relay_count_24h')} last 24h")
        if n.get("battery_mv"):
            extra.append(f"battery {n['battery_mv'] / 1000:.2f}V")
        if n.get("temperature_c") is not None:
            extra.append(f"temp {n['temperature_c']}C")
        if n.get("usefulness_grade"):
            extra.append(f"usefulness grade {n['usefulness_grade']}")
        if n.get("advert_count") is not None:
            extra.append(f"{n['advert_count']} adverts")
        lines.append("; ".join(extra))
        try:
            nb = (await self.cs(f"/api/nodes/{pk}/neighbors")).get("neighbors") or []
            nb = [x for x in nb if x.get("role") != "observer"] or nb
            nb.sort(key=lambda x: -(x.get("count") or 0))
            if nb:
                lines.append("Neighbors (as the network sees them): " + "; ".join(
                    f"{x.get('name') or x.get('prefix')}"
                    + (f" SNR {x['avg_snr']:.1f}" if isinstance(x.get("avg_snr"), (int, float)) else "")
                    + (f" {x['distance_km'] * (0.621371 if self._units() == 'imperial' else 1):.1f}"
                       f"{'mi' if self._units() == 'imperial' else 'km'}"
                       if isinstance(x.get("distance_km"), (int, float)) else "")
                    for x in nb[:5]))
        except T.ToolError:
            pass
        if others:
            lines.append("Other matches: " + "; ".join(f"{o.get('name')} [{o.get('role')}]" for o in others))
        return "\n".join(lines), {"found": 1 + len(others), "node": n.get("name")}

    async def nodes_list(self, role: str = "", heard_within_h: float | None = None, sort: str = "recent",
                         limit: int = 10, first_seen_within_h: float | None = None) -> tuple[str, dict]:
        nodes = await self.nodes()
        now = time.time()
        role = (role or "").strip().lower().rstrip("s")
        if role and role not in ROLES:
            raise T.ToolError(f"role must be one of {', '.join(ROLES)}")
        sel = [n for n in nodes if not role or n.get("role") == role]
        if heard_within_h:
            cut = now - float(heard_within_h) * 3600
            sel = [n for n in sel if (_ts(n.get("last_heard") or n.get("last_seen")) or 0) >= cut]
        if first_seen_within_h:
            cut = now - float(first_seen_within_h) * 3600
            sel = [n for n in sel if (_ts(n.get("first_seen")) or 0) >= cut]
        home = await self.home()
        if sort == "new":
            sel.sort(key=lambda n: -(_ts(n.get("first_seen")) or 0))
        elif sort == "busiest":
            sel.sort(key=lambda n: -(n.get("relay_count_24h") or 0))
        elif sort == "nearest" and home:
            def km(n):
                if not n.get("lat") or not n.get("lon"):
                    return 1e9
                return math.hypot(float(n["lat"]) - home[0],
                                  (float(n["lon"]) - home[1]) * math.cos(math.radians(home[0])))
            sel.sort(key=km)
        else:
            sel.sort(key=lambda n: -(_ts(n.get("last_heard") or n.get("last_seen")) or 0))
        limit = max(1, min(int(limit or 10), 10))
        desc = (f"{len(sel)} {role or 'node'}s" + (f" heard in the last {heard_within_h:g}h" if heard_within_h else "")
                + (f" first seen in the last {first_seen_within_h:g}h" if first_seen_within_h else ""))
        lines = [desc + f" (of {len(nodes)} known). Top {min(limit, len(sel))} by {sort}:"]
        for n in sel[:limit]:
            line = self._node_line(n, home, now)
            if sort == "new":
                line += f", first seen {ago(n.get('first_seen'), now)}"
            if sort == "busiest":
                line += f", {n.get('relay_count_24h') or 0} relays/24h"
            lines.append("- " + line)
        return "\n".join(lines), {"count": len(sel)}

    async def overview(self) -> tuple[str, dict]:
        s = await self.cs("/api/stats")
        c = s.get("counts") or {}
        lines = [f"Network map: {s.get('totalNodes')} nodes ({c.get('repeaters', '?')} repeaters, "
                 f"{c.get('companions', '?')} companions, {c.get('rooms', '?')} rooms). "
                 f"Packets: {s.get('packetsLastHour')} last hour, {s.get('packetsLast24h')} last 24h."]
        nodes = await self.nodes()
        now = time.time()
        for h in (1, 24):
            n = [x for x in nodes if (_ts(x.get("last_heard") or x.get("last_seen")) or 0) >= now - h * 3600]
            lines.append(f"Heard in last {h}h: {len(n)} nodes ({sum(1 for x in n if x.get('role') == 'repeater')} "
                         f"repeaters).")
        try:
            obs = (await self.cs("/api/observers")).get("observers") or []
            mins = float(self._m("online_minutes"))
            on = [o for o in obs if (_ts(o.get("last_seen")) or 0) >= now - mins * 60]
            lines.append(f"Observers feeding the map: {len(on)} of {len(obs)} online (last {mins:g} min).")
        except T.ToolError:
            pass
        busy = sorted(nodes, key=lambda n: -(n.get("relay_count_1h") or 0))[:3]
        if busy and (busy[0].get("relay_count_1h") or 0) > 0:
            lines.append("Busiest relays last hour: " + ", ".join(
                f"{n.get('name')} ({n.get('relay_count_1h')})" for n in busy if n.get("relay_count_1h")))
        return "\n".join(lines), {}

    async def home_status(self) -> tuple[str, dict]:
        st = await self.oh("stats")
        lines = []
        name = st.get("node_name") or "home repeater"
        bits = [f"{name} (our repeater)", f"mode {st.get('mode', '?')}"]
        if st.get("uptime_seconds") is not None:
            bits.append(f"up {float(st['uptime_seconds']) / 86400:.1f}d")
        if st.get("radio_status"):
            bits.append(f"radio {st['radio_status']}")
        lines.append(", ".join(bits))
        nums = []
        for k, label in (("rx_count", "rx"), ("rx_per_hour", "rx/h"), ("forwarded_count", "forwarded"),
                         ("forwarded_per_hour", "fwd/h"), ("dropped_count", "dropped"),
                         ("noise_floor_dbm", "noise floor dBm"), ("crc_error_count", "CRC errors")):
            v = st.get(k)
            if isinstance(v, (int, float)):
                nums.append(f"{label} {round(v, 1) if isinstance(v, float) else v}")
        if nums:
            lines.append(", ".join(nums))
        dc = st.get("duty_cycle")
        if isinstance(dc, dict) and isinstance(dc.get("utilization_percent"), (int, float)):
            lines.append(f"airtime use {dc['utilization_percent']:.1f}%")
        try:
            nf = await self.oh("noise_floor_stats", {"hours": 24})
            stats = nf.get("stats") if isinstance(nf, dict) else None
            if isinstance(stats, dict):
                parts = [f"{k} {round(v, 1)}" for k, v in stats.items() if isinstance(v, (int, float))][:4]
                if parts:
                    lines.append("Noise floor 24h (dBm): " + ", ".join(parts))
        except T.ToolError:
            pass
        nb = st.get("neighbors")
        if isinstance(nb, (list, dict)):
            lines.append(f"Direct neighbors known: {len(nb)}")
        return "\n".join(lines), {"node": name}

    async def home_neighbors(self) -> tuple[str, dict]:
        d = await self.oh("neighbor_links", {"active_within_seconds": 3600, "limit": 50})
        rows = d.get("links") or d.get("neighbors") or d.get("rows") if isinstance(d, dict) else d
        rows = rows if isinstance(rows, list) else []
        nodes = await self.nodes()
        out = []
        for r in rows[:15]:
            if not isinstance(r, dict):
                continue
            nm = r.get("name") or r.get("node_name") or r.get("neighbor_name")
            pk = str(r.get("pubkey") or r.get("public_key") or r.get("neighbor_hash") or r.get("hash") or "")
            if not nm and pk:
                nm = self._names_by_prefix(nodes, pk)
            snr = next((r[k] for k in ("snr", "avg_snr", "last_snr") if isinstance(r.get(k), (int, float))), None)
            seen = next((r[k] for k in ("last_seen", "last_heard", "last_seen_ts") if r.get(k)), None)
            out.append(f"{nm or '?'}" + (f" SNR {snr:.1f}" if snr is not None else "")
                       + (f" {ago(seen)}" if seen else ""))
        if not out:
            return "Our repeater heard no direct neighbors in the last hour.", {"count": 0}
        return f"Our repeater's direct neighbors (last hour, {len(rows)}): " + "; ".join(out), {"count": len(rows)}

    async def _is_home(self, pk: str) -> bool:
        if not self.cfg.openhop_api_key:
            return False
        try:
            return str((await self.oh("stats")).get("public_key") or "").lower() == pk.lower()
        except T.ToolError:
            return False

    # ---------- hops ----------
    async def hops(self, name: str, live: bool, radio_ok: bool) -> tuple[str, dict]:
        best, others = await self.resolve(name)
        if not best:
            return f"No node matching '{name}' is known.", {"found": 0}
        pk, nm = best["public_key"], best.get("name")
        nodes = await self.nodes()

        def path_names(path_hex: str, hlen: int) -> str:
            step = max(1, hlen) * 2
            hops = [path_hex[i:i + step] for i in range(0, len(path_hex), step)]
            return " > ".join(self._names_by_prefix(nodes, h) for h in hops if h)

        lines = []
        if self.mesh is not None and getattr(self.mesh, "connected", False):
            try:
                c = await self.mesh.get_contact(pk)
                if c and isinstance(c.get("out_path_len"), int) and c["out_path_len"] >= 0:
                    n = c["out_path_len"]
                    lines.append(f"Known direct route from {self.cfg.bot_name} to {nm}: {n} hop{'s' * (n != 1)}"
                                 + (f" via {path_names(c.get('out_path', ''), c.get('out_path_hash_mode', 0) + 1)}"
                                    if n else ""))
            except Exception as e:
                log.debug("contact lookup failed: %s", e)
            try:
                ap = await self.mesh.advert_path(pk)
                if ap:
                    n = ap["path_len"]
                    lines.append(f"{nm}'s last advert reached us in {n} hop{'s' * (n != 1)}"
                                 + (f" via {path_names(ap.get('path', ''), ap.get('path_hash_mode', 0) + 1)}"
                                    if n else " (heard directly)")
                                 + (f", {ago(ap.get('timestamp'))}" if ap.get("timestamp") else ""))
            except Exception as e:
                log.debug("advert path lookup failed: %s", e)
        if live:
            if not radio_ok:
                lines.append("(Live path discovery is turned off on this channel.)")
            else:
                text = await self._radio("path", best, lambda: self._path_discovery(best, path_names))
                lines.append(text)
        if not lines:
            lines.append(f"No route to {nm} is known yet (its adverts haven't reached us, or only by flood)."
                         + (" A live path discovery could find one." if radio_ok and not live else ""))
        heard = ago(best.get("last_heard") or best.get("last_seen"))
        lines.append(f"{nm} [{best.get('role')}] was last heard by the network {heard}.")
        if others:
            lines.append("Other matches: " + "; ".join(str(o.get("name")) for o in others))
        return "\n".join(lines), {"node": nm}

    async def _path_discovery(self, node: dict, path_names) -> tuple[str, dict]:
        await self.mesh.ensure_contact(node)
        tries = int(self._m("radio_attempts"))
        r = await self.mesh.path_discovery(node["public_key"], float(self._m("radio_timeout_s")), tries)
        if not r:
            raise TimeoutError(f"no path response after {tries} tries (out of range or busy)")
        n = r.get("out_path_len", 0)
        via = path_names(r.get("out_path", ""), r.get("out_path_hash_len", 1))
        return (f"Live path discovery: {node.get('name')} is {n} hop{'s' * (n != 1)} from {self.cfg.bot_name}"
                + (f" via {via}" if n else " (direct)")), {}

    # ---------- radio: guest login ----------
    def guest_password(self, name: str) -> str:
        return self.cfg.guest_password(name) or str(self._m("default_guest_password") or "")

    async def _radio(self, kind: str, node: dict, fn) -> str:
        """Run one transmitting query with the hourly cap, a per-node cache and one at a time."""
        key = f"{kind}:{node['public_key']}"
        hit = self._radio_cache.get(key)
        if hit and time.time() - hit[0] < float(self._m("radio_cache_s")):
            return hit[1] + f" (from {ago(hit[0])})"
        if self.mesh is None or not self.mesh.connected:
            raise T.ToolError("the radio companion is not connected")
        cap = int(self._m("radio_max_per_hour"))
        if self.radio_used_last_hour() >= cap:
            raise T.ToolError(f"radio query limit reached ({cap}/hour); try again later")
        async with self._radio_lock:
            t0 = time.time()
            try:
                text, _ = await fn()
                outcome = "ok"
            except PermissionError as e:
                outcome = str(e)
                raise T.ToolError(str(e))
            except (TimeoutError, asyncio.TimeoutError) as e:
                outcome = str(e) or "timeout"
                raise T.ToolError(outcome)
            except T.ToolError as e:
                outcome = str(e)
                raise
            except Exception as e:
                outcome = f"{type(e).__name__}: {e}"
                raise T.ToolError(f"radio query failed: {outcome}"[:200])
            finally:
                self.radio_log.append((t0, kind, node.get("name"), outcome[:120]))
                log.info("radio %s %s: %s (%.1fs)", kind, node.get("name"), outcome[:120], time.time() - t0)
        self._radio_cache[key] = (time.time(), text, {})
        return text

    async def _login_query(self, node: dict, what: str) -> tuple[str, dict]:
        pk = node["public_key"]
        timeout = float(self._m("radio_timeout_s"))
        await self.mesh.ensure_contact(node)
        tries = int(self._m("radio_attempts"))
        login = await self.mesh.login(pk, self.guest_password(str(node.get("name") or "")), timeout, tries)
        try:
            if what == "neighbors":
                r = await self.mesh.neighbours(pk, timeout, tries)
                if r is None:
                    raise TimeoutError(f"logged in, but no neighbour list came back after {tries} tries")
                nodes = await self.nodes()
                nb = sorted(r.get("neighbours") or [], key=lambda x: x.get("secs_ago", 1e9))
                items = [f"{self._names_by_prefix(nodes, x.get('pubkey', ''))} SNR {x.get('snr', 0):.1f} "
                         f"{ago(time.time() - (x.get('secs_ago') or 0))}" for x in nb[:15]]
                total = r.get("neighbours_count", len(nb))
                return (f"{node.get('name')} reports {total} neighbour{'s' * (total != 1)} (radio, logged in as "
                        f"{'admin' if login.get('is_admin') else 'guest'}): " + ("; ".join(items) or "none")), {}
            r = await self.mesh.status_req(pk, timeout, tries)
            if r is None:
                raise TimeoutError(f"logged in, but no status came back after {tries} tries")
            bits = [f"up {r['uptime'] / 86400:.1f}d" if r.get("uptime") else "",
                    f"battery {r['bat'] / 1000:.2f}V" if r.get("bat") else "",
                    f"noise floor {r['noise_floor']}dBm" if r.get("noise_floor") else "",
                    f"last SNR {r['last_snr']:.1f}" if r.get("last_snr") is not None else "",
                    f"rx {r.get('nb_recv')} tx {r.get('nb_sent')}" if r.get("nb_recv") is not None else "",
                    f"tx queue {r['tx_queue_len']}" if r.get("tx_queue_len") else ""]
            return f"{node.get('name')} status (radio): " + ", ".join(b for b in bits if b), {}
        finally:
            await self.mesh.logout(pk)

    async def _remote(self, name: str, what: str, radio_ok: bool) -> tuple[str, dict]:
        best, others = await self.resolve(name)
        if not best:
            return f"No node matching '{name}' is known.", {"found": 0}
        if await self._is_home(best["public_key"]):
            return await (self.home_neighbors() if what == "neighbors" else self.home_status())
        lines, meta = [], {"node": best.get("name")}
        if best.get("role") not in ("repeater", "room"):
            lines.append(f"{best.get('name')} is a {best.get('role')}, not a repeater, so it can't be queried.")
        elif not radio_ok:
            lines.append("(Radio queries are off on this channel, so this is the network map's view only.)")
        else:
            try:
                lines.append(await self._radio(what, best, lambda: self._login_query(best, what)))
                meta["radio"] = True
            except T.ToolError as e:
                lines.append(f"Radio query to {best.get('name')} failed: {e}. Network map view instead:")
                meta["radio_error"] = str(e)[:120]
        if not meta.get("radio"):
            text, _ = await self.node(best["public_key"][:16])
            lines.append(text)
        elif others:
            lines.append("Other matches: " + "; ".join(str(o.get("name")) for o in others))
        return "\n".join(lines), meta

    # ---------- dispatch ----------
    async def run(self, name: str, args: dict, channel: dict | None) -> tuple[str, dict]:
        radio_ok = bool((channel or {}).get("radio_queries"))
        target = str(args.get("name") or args.get("node") or "").strip()
        if name == "mesh_node":
            return await self.node(target)
        if name == "mesh_nodes":
            def num(k):
                v = args.get(k)
                return float(v) if v not in (None, "") else None
            return await self.nodes_list(str(args.get("role") or ""), num("heard_within_h"),
                                         str(args.get("sort") or "recent"), int(args.get("limit") or 10),
                                         num("first_seen_within_h"))
        if name == "mesh_overview":
            return await self.overview()
        if name == "mesh_hops":
            return await self.hops(target, bool(args.get("live")), radio_ok)
        if name == "repeater_status":
            return await (self._remote(target, "status", radio_ok) if target else self.home_status())
        if name == "repeater_neighbors":
            return await (self._remote(target, "neighbors", radio_ok) if target else self.home_neighbors())
        raise T.ToolError(f"unknown tool {name}")

    async def test(self) -> list[dict]:
        """Connectivity check for the web UI. Never transmits."""
        out = []
        try:
            s = await self.cs("/api/stats", ttl=0)
            out.append({"source": "CoreScope", "ok": True, "detail": f"{s.get('totalNodes')} nodes, "
                                                                     f"version {s.get('version')}"})
        except Exception as e:
            out.append({"source": "CoreScope", "ok": False, "detail": str(e)})
        if not self.cfg.openhop_api_key:
            out.append({"source": "openHop", "ok": False, "detail": "no API key set"})
        else:
            try:
                self._cache.clear()
                st = await self.oh("stats")
                out.append({"source": "openHop", "ok": True,
                            "detail": f"{st.get('node_name')}, mode {st.get('mode')}"})
            except Exception as e:
                out.append({"source": "openHop", "ok": False, "detail": str(e)})
        return out
