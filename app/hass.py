"""Home Assistant tools, passed through from the Home Assistant MCP server registered in LiteLLM.

The tools are discovered at runtime (GET /mcp-rest/tools/list for that server) and offered to the model with an
"ha_" prefix; calls go to POST /mcp-rest/tools/call. Per channel, `home_assistant` is off (default), read (only
tools in read_tools) or control (read_tools + control_tools). Every other tool (admin: restart, backups, config
editing, deletes...) is hidden. Service calls are checked against blocked_services (empty by default) before
they run. Only private channels get access: hashtag channels are always off.

The channel's secret is the real access control: on MeshCore a sender name is only a text prefix, so anyone who
has the channel key can claim to be anyone.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import time

from . import tools as T

log = logging.getLogger("meshbot.hass")

PREFIX = "ha_"
MODES = ("off", "read", "control")


def _pat_match(name: str, pat: str, substring: bool) -> bool:
    n, p = name.lower(), pat.strip().lower()
    if not p:
        return False
    if any(c in p for c in "*?["):
        return fnmatch.fnmatch(n, p)
    return p in n if substring else n == p


def matches(name: str, patterns, substring: bool = False) -> bool:
    return any(_pat_match(name, p, substring) for p in patterns or [])


def tool_status(name: str, read_tools, control_tools, blocked_tools) -> str:
    """blocked | read | control | hidden. Only read and control tools are ever offered; everything else
    (admin tools: restart, backups, config editing, deletes...) stays hidden unless added to a list."""
    if matches(name, blocked_tools, substring=True):
        return "blocked"
    if matches(name, read_tools):
        return "read"
    if matches(name, control_tools):
        return "control"
    return "hidden"


def blocked_call(args: dict, blocked_services) -> str | None:
    """For service-calling tools: the domain.service (or entity.action) a call would run, if it's blocked."""
    if args.get("ws_command"):
        return "raw websocket commands"
    targets = []
    if args.get("domain") or args.get("service"):
        svc = f"{args.get('domain') or ''}.{args.get('service') or ''}"
        if "." in str(args.get("service") or "") and not args.get("domain"):
            svc = str(args["service"])
        targets.append(svc)
    for op in args.get("operations") or []:
        if isinstance(op, dict):
            ent = str(op.get("entity_id") or "")
            targets.append(f"{ent.split('.')[0]}.{op.get('action') or op.get('service') or ''}")
            targets.append(ent)
    if args.get("action") and args.get("selector"):
        sel = json.dumps(args.get("selector")).lower()
        targets.append(f"{args['action']}")
        targets.extend(d + "." + str(args["action"]) for d in ("lock", "alarm_control_panel") if d in sel)
    ents = args.get("entity_id")
    for e in ([ents] if isinstance(ents, str) else ents or []):
        targets.extend(x.strip() for x in str(e).split(","))
    for t in targets:
        if t and matches(t, blocked_services, substring=False):
            return t
    return None


def channel_mode(channel: dict | None) -> str:
    """Home Assistant access for a channel. Hashtag channels never get any (anyone can join them by name), nor DMs."""
    ch = channel or {}
    if str(ch.get("name") or "").startswith(("#", "@")) or ch.get("dm"):
        return "off"  # hashtag channels: anyone can join; DMs: off by design
    mode = str(ch.get("home_assistant") or "off")
    return mode if mode in MODES else "off"


def safe_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return (n if n.lower().startswith(PREFIX) else PREFIX + n)[:64]


def trim_schema(obj, limit: int = 160):
    """Shorten every nested 'description' (this server's schemas are ~285k chars in total)."""
    if isinstance(obj, dict):
        return {k: (v[:limit] if k == "description" and isinstance(v, str) else trim_schema(v, limit))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [trim_schema(x, limit) for x in obj]
    return obj


def short_desc(text: str, limit: int = 400) -> str:
    first = re.split(r"\n\s*\n", (text or "").strip(), maxsplit=1)[0]
    return " ".join(first.split())[:limit]


class HassTools:
    def __init__(self, brain):
        self.brain = brain
        self.cfg = brain.cfg
        self._server: tuple[str, str] | None = None     # (configured name, server_id)
        self._tools: tuple[float, str, list[dict]] | None = None  # (time, server_id, raw tools)
        self.last_error: str | None = None

    def _h(self) -> dict:
        key = self.cfg.llm_api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _s(self, k):
        return self.cfg.value(f"home_assistant.{k}")

    async def servers(self) -> list[dict]:
        r = await self.brain.client.get(self.brain._proxy_base() + "/v1/mcp/server", headers=self._h(), timeout=15)
        r.raise_for_status()
        return [s for s in r.json() if isinstance(s, dict)]

    async def server_id(self) -> str:
        want = str(self._s("mcp_server") or "").strip()
        if self._server and self._server[0] == want:
            return self._server[1]
        servers = await self.servers()

        def names(s):
            return {str(s.get(k) or "").lower() for k in ("server_name", "alias", "server_id")} - {""}
        if want:
            hit = [s for s in servers if want.lower() in names(s)]
        else:  # auto-detect
            hit = [s for s in servers if any(re.search(r"home.?assistant|hass|^ha$|^ha[_-]", n) for n in names(s))]
        if not hit:
            seen = ", ".join(sorted(str(s.get("server_name") or s.get("alias")) for s in servers)) or "none"
            raise T.ToolError(f"LiteLLM shows no Home Assistant MCP server for this API key"
                              f"{f' named {want!r}' if want else ''} (visible: {seen}). Grant the key access to it "
                              f"in LiteLLM, or set the server name in Settings > Home Assistant.")
        sid = hit[0]["server_id"]
        self._server = (want, sid)
        return sid

    async def raw_tools(self, force: bool = False) -> list[dict]:
        sid = await self.server_id()
        ttl = float(self._s("cache_s"))
        if not force and self._tools and self._tools[1] == sid and time.time() - self._tools[0] < ttl:
            return self._tools[2]
        r = await self.brain.client.get(self.brain._proxy_base() + "/mcp-rest/tools/list",
                                        params={"server_id": sid}, headers=self._h(), timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise T.ToolError(f"LiteLLM: {data.get('message') or data.get('error')}"[:200])
        tools = [t for t in data.get("tools") or [] if t.get("name")]
        self._tools = (time.time(), sid, tools)
        self.last_error = None
        return tools

    def classify(self, tools: list[dict]) -> list[dict]:
        """[{name, ha_name, status, read, blocked, description}] for the UI and for filtering."""
        lists = [list(self._s(k) or []) for k in ("read_tools", "control_tools", "blocked_tools")]
        out = []
        for t in tools:
            st = tool_status(t["name"], *lists)
            out.append({"name": t["name"], "ha_name": safe_name(t["name"]), "status": st, "read": st == "read",
                        "blocked": st == "blocked", "description": short_desc(t.get("description") or "", 200)})
        return out

    async def defs_for(self, channel: dict | None) -> list[dict]:
        """OpenAI tool definitions this channel may use (empty when off or unavailable)."""
        mode = channel_mode(channel)
        if mode not in ("read", "control"):
            return []
        try:
            tools = await self.raw_tools()
        except Exception as e:
            self.last_error = str(e)[:300]
            log.warning("Home Assistant tools unavailable: %s", e)
            return []
        info = {c["name"]: c for c in self.classify(tools)}
        allowed = ("read",) if mode == "read" else ("read", "control")
        out = []
        for t in tools:
            c = info[t["name"]]
            if c["status"] not in allowed:
                continue
            params = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
            if not isinstance(params, dict) or params.get("type") != "object":
                params = {"type": "object", "properties": {}}
            out.append({"type": "function", "function": {
                "name": c["ha_name"], "description": "[Home Assistant] " + short_desc(t.get("description") or
                                                                                       t["name"]),
                "parameters": trim_schema(params)}})
        return out

    async def call(self, ha_name: str, args: dict, channel: dict | None) -> tuple[str, dict]:
        mode = channel_mode(channel)
        tools = await self.raw_tools()
        info = {c["ha_name"]: c for c in self.classify(tools)}
        c = info.get(ha_name)
        # re-check at call time: the model only gets offered tools, but never trust the name it sends back
        allowed = {"read": ("read",), "control": ("read", "control")}.get(mode, ())
        if not c or c["status"] not in allowed:
            raise T.ToolError("that Home Assistant tool isn't allowed on this channel")
        if c["status"] == "control":
            bad = blocked_call(args, list(self._s("blocked_services") or []))
            if bad:
                log.warning("home assistant: refused blocked call %s %s", c["name"], bad)
                raise T.ToolError(f"{bad} is blocked for mesh use")
        sid = await self.server_id()
        r = await self.brain.client.post(self.brain._proxy_base() + "/mcp-rest/tools/call", headers=self._h(),
                                         json={"name": c["name"], "arguments": args, "server_id": sid}, timeout=30)
        if r.status_code >= 400:
            raise T.ToolError(f"Home Assistant call failed: HTTP {r.status_code} {r.text[:150]}")
        data = r.json()
        texts = []
        for item in data.get("content") or []:
            if isinstance(item, dict) and item.get("text"):
                texts.append(item["text"])
        text = "\n".join(texts) or json.dumps(data)[:500]
        if data.get("isError"):
            raise T.ToolError(f"Home Assistant: {text[:200]}")
        limit = int(self._s("max_result_chars"))
        if len(text) > limit:
            text = text[:limit] + "\n[truncated]"
        log.info("home assistant %s %s(%s)", "action" if not c["read"] else "read", c["name"],
                 json.dumps(args)[:200])
        return text, {"ha_tool": c["name"], "action": not c["read"]}

    async def overview(self) -> dict:
        """For the Settings page: the server found and how each tool is classified."""
        try:
            sid = await self.server_id()
            tools = self.classify(await self.raw_tools(force=True))
            if not tools:
                raise T.ToolError(f"LiteLLM found the server ({sid}) but it returned no tools. Usually LiteLLM can't "
                                  "authenticate to Home Assistant: in LiteLLM, set the server's auth to a bearer "
                                  "token with a Home Assistant long-lived access token, and check its URL ends in "
                                  "/api/mcp.")
            return {"ok": True, "server_id": sid, "tools": tools}
        except Exception as e:
            self.last_error = str(e)[:300]
            return {"ok": False, "error": str(e)[:400], "tools": []}
