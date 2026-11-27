"""LiteLLM (OpenAI-compatible) calls: decide + answer in one shot, and a shorten re-ask."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .compress import part_budget
from .tools import enabled_tools
from .history import Msg
from . import tools as T
from .hass import HassTools

log = logging.getLogger("meshbot.brain")

EAGERNESS = {
    "low": "Be reserved. Reply only when you are addressed by name or asked a clear direct question nobody answered.",
    "normal": "Reply when there is a genuine question or request you can help with; stay out of social chatter.",
    "high": "Be helpful and forthcoming. Reply to most questions and requests, but still skip pure chit-chat.",
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "respond": {"type": "boolean"},
        "reason": {"type": "string"},
        "parts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["respond", "reason", "parts"],
    "additionalProperties": False,
}


class TokenLimit(RuntimeError):
    """The model hit its token limit mid-answer (usually thinking eating the budget)."""


@dataclass
class Decision:
    respond: bool = False
    reason: str = ""
    parts: list[str] = field(default_factory=list)
    raw: str = ""
    latency_s: float = 0.0
    usage: dict = field(default_factory=dict)
    error: str | None = None
    searches: list = field(default_factory=list)  # [{"query", "results", "ms", "error"?}]
    model: str = ""                                # model that produced this decision
    fallback: bool = False                         # True when it wasn't the channel's primary
    attempts: list = field(default_factory=list)   # models tried before this one: {"model", "error"|"skipped"}
    error_kind: str = ""                           # service (unreachable/HTTP/timeout) | content (bad output)
    system: str = ""   # exact prompts sent (shown in the playground, not logged)
    user: str = ""


def format_thread(msgs: list[Msg], bot_name: str) -> str:
    lines = []
    for i, m in enumerate(msgs):
        t = datetime.fromtimestamp(m.ts).strftime("%H:%M")
        who = f"{bot_name} (you)" if m.own else m.sender
        if not m.own and getattr(m, "hops", None) is not None:
            who += " (direct)" if m.hops == 0 else f" ({m.hops} hop{'s' * (m.hops != 1)})"
        mark = ">>> " if i == len(msgs) - 1 else ""
        lines.append(f"{mark}[{t}] {who}: {m.text}")
    return "\n".join(lines)


def tool_rules(channel: dict, max_calls: int) -> str:
    ha = [d["function"]["name"] for d in channel.get("_ha_defs") or []]
    names = (enabled_tools(channel) + ha) if max_calls else []
    if not names:
        return ""
    out = [f"- Tools available: {', '.join(names)} (at most {max_calls} calls in total). Decide whether to respond "
           "first. If you will respond and the answer depends on current, local or specific facts (news, weather, "
           "scores, opening hours, prices, schedules, recent events, local businesses), you MUST call the tools "
           "before answering. Never answer 'I can't check' or 'see website X' without trying the tools first. "
           "Skip tools for general knowledge you already know well."]
    if "weather" in names:
        out.append("- Any weather question: call weather (not web_search). Default location: "
                   f"{channel.get('weather_location') or 'the asker did not say'}.")
    if "read_page" in names:
        out.append("- Search snippets are rarely enough. After web_search, read_page the 1-2 most promising results "
                   "(official or primary sources first) and answer from what the page says. If a page fails or is "
                   "nearly empty, read the next result instead. Name the source briefly when it helps. Only if the "
                   "tools truly come up empty, say so and point to the best source.")
    if "mesh_node" in names:
        radio = ("Radio queries are ON: naming a repeater in repeater_status/repeater_neighbors logs in to it "
                 "over the air, and mesh_hops live=true runs a path discovery. These use airtime, so only when "
                 "asked." if channel.get("radio_queries") else
                 "Radio queries are OFF here: named repeaters are answered from the network map only.")
        out.append("- Questions about the mesh itself (a node or repeater, who is up, last heard, hops, coverage, "
                   "neighbors, our repeater, network activity): you MUST use the mesh tools, never guess. "
                   "'Our/your/the home repeater' = repeater_status or repeater_neighbors with no name. In the thread, "
                   "'(N hops)' after a sender's name means N repeaters relayed their message to you; '(direct)' "
                   "means you heard them with no repeater in between. "
                   + radio)
    if ha:
        control = channel.get("home_assistant") == "control"
        if control:
            out.append("- Home Assistant tools (ha_*) control the owner's home. This is the household's private "
                       "channel: everyone on it is authorized to control every device. When the newest message asks "
                       "for something, do it right away with the tools (search for the entity if needed, then act). "
                       "Never ask for confirmation, never question who is asking, never refuse for safety reasons. "
                       "If several devices could match, use the most likely one. Then say briefly what you did.")
        else:
            out.append("- Home Assistant tools (ha_*) read states in the owner's home (sensors, lights, climate). "
                       "Look up entities instead of guessing. You can't change anything here; say so if asked.")
    return "\n".join(out) + "\n"


def runtime_rules(bot_name: str, channel: dict, now: datetime | None = None, max_searches: int = 0) -> str:
    now = now or datetime.now().astimezone()
    ptb, maxp = int(channel["packet_text_bytes"]), int(channel["max_parts"])
    one = part_budget(ptb, bot_name, 1)
    many = part_budget(ptb, bot_name, maxp)
    eager = EAGERNESS.get(str(channel.get("eagerness", "normal")), EAGERNESS["normal"])
    return (
        "## Runtime facts\n"
        f"- Your name on the mesh: {bot_name}. Channel: {channel['name']}.\n"
        f"- Local time now: {now.strftime('%A %Y-%m-%d %H:%M %Z')}.\n"
        f"- Eagerness: {eager}\n"
        + tool_rules(channel, max_searches)
        + "\n"
        "## Output format\n"
        'Reply with ONLY a JSON object: {"respond": true|false, "reason": "<few words why>", "parts": ["..."]}\n'
        "- parts is [] when respond is false.\n"
        f"- One part may be at most {one} characters. If you need several, each at most {many} characters, "
        f"and never more than {maxp} parts. Prefer one part.\n"
        "- Do not number the parts; that is added for you."
    )


def parse_json(content: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S | re.I).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


OFFLINE_NOTE = ("\n- NOTE: your primary model was unreachable, so the internet may be down. If tools fail, answer "
                "from your own knowledge and briefly say live data is unavailable right now.")


class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = httpx.AsyncClient()
        # Per-model downgrades learned from HTTP 400s, so we don't retry known-bad params every call.
        self._learned: dict[str, dict] = {}
        self._learned_sig = None
        # Per-model health for failover: ok, last_ok, last_error, error_time, down_until.
        self.health: dict[str, dict] = {}
        self.meshtools = None  # set by Bot (needs the companion connection)
        self.hass = HassTools(self)

    async def close(self):
        await self.client.aclose()

    def _learn(self, model: str, llm: dict) -> dict:
        sig = (llm["base_url"], llm["response_format"])
        if sig != self._learned_sig:  # settings changed in the UI: forget learned downgrades
            self._learned, self._learned_sig = {}, sig
        return self._learned.setdefault(model, {"fmt": None, "no_kwargs": False})

    async def _chat(self, messages: list[dict], schema: dict | None, tools: list | None = None,
                    tool_choice: str = "auto", model: str | None = None,
                    timeout: float | None = None) -> tuple[str, dict, list]:
        """One completion. Returns (content, usage, tool_calls). Offering tools (tool_choice auto) drops
        response_format, because forced JSON output would stop the model from calling a tool."""
        llm = self.cfg.section("llm")
        model = model or llm["model"]
        if not model:
            raise RuntimeError("no model is set: choose one in Settings > Model")
        learned = self._learn(model, llm)
        url = str(llm.get("base_url", "")).rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        key = self.cfg.llm_api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        fmts = ["json_schema", "json_object", "none"]
        fmt = learned["fmt"] or str(llm.get("response_format", "json_schema"))
        fmt_i = fmts.index(fmt) if fmt in fmts else 2
        while True:
            body = {"model": model, "messages": messages,
                    "temperature": float(llm.get("temperature", 0.4)),
                    "max_tokens": self._max_tokens(llm)}
            f = "none" if tools and tool_choice != "none" else fmts[fmt_i]
            if tools:
                body["tools"], body["tool_choice"] = tools, tool_choice
            if f == "json_schema" and schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "reply", "schema": schema, "strict": True}}
            elif f in ("json_schema", "json_object"):
                body["response_format"] = {"type": "json_object"}
            if not learned["no_kwargs"]:
                body["chat_template_kwargs"] = {"enable_thinking": bool(llm.get("thinking", False))}
            r = await self.client.post(url, json=body, headers=headers,
                                       timeout=float(timeout or llm.get("timeout_s", 60)))
            if r.status_code == 400:
                if not learned["no_kwargs"]:
                    log.info("%s: 400 with chat_template_kwargs, retrying without", model)
                    learned["no_kwargs"] = True
                    continue
                if fmt_i < 2 and f != "none":
                    fmt_i += 1
                    learned["fmt"] = fmts[fmt_i]
                    log.warning("%s: 400, falling back to response_format=%s: %s", model, fmts[fmt_i], r.text[:200])
                    continue
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code} from LiteLLM for {model}: {r.text[:250]}",
                                            request=r.request, response=r)
            data = r.json()
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            tool_calls = choice["message"].get("tool_calls") or []
            if choice.get("finish_reason") == "length":
                which = "thinking_max_tokens" if llm.get("thinking") else "max_tokens"
                raise TokenLimit(f"model ran out of tokens ({which}={self._max_tokens(llm)}) before finishing its "
                                 f"answer; raise {which} in Settings > Model" + (", or turn thinking off"
                                 if llm.get("thinking") else "") + f". Partial output: {content[:80]!r}")
            return content, data.get("usage") or {}, tool_calls

    # ---------- failover ----------
    def model_chain(self, channel: dict | None) -> list[str]:
        """[primary] or [primary, fallback] for a channel (blank = Settings > Model, 'none' = no fallback)."""
        llm = self.cfg.section("llm")
        primary = str((channel or {}).get("model") or llm["model"]).strip()
        fb = str((channel or {}).get("fallback_model") or llm.get("fallback_model") or "").strip()
        chain = [primary]
        if fb and fb.lower() != "none" and fb != primary:
            chain.append(fb)
        return chain

    def held(self, model: str) -> bool:
        return self.health.get(model, {}).get("down_until", 0) > time.time()

    def mark_ok(self, model: str) -> None:
        self.health.setdefault(model, {}).update(ok=True, last_ok=time.time(), down_until=0)

    def mark_fail(self, model: str, error: str, hold_s: float = 0) -> None:
        now = time.time()
        self.health.setdefault(model, {}).update(ok=False, last_error=error[:300], error_time=now,
                                                 down_until=now + hold_s if hold_s else 0)

    async def ping(self, model: str | None = None) -> dict:
        """Tiny round trip to check the endpoint, key and model. Success clears a failover hold."""
        model = model or self.cfg.value("llm.model")
        t0 = time.monotonic()
        content, usage, _ = await self._chat([{"role": "user", "content": "Reply with the word OK."}], None,
                                             model=model)
        self.mark_ok(model)
        return {"model": model, "reply": content.strip()[:200], "latency_s": round(time.monotonic() - t0, 2),
                "usage": usage}

    @staticmethod
    def _max_tokens(llm: dict) -> int:
        return int(llm.get("thinking_max_tokens") or 4000) if llm.get("thinking") else int(llm.get("max_tokens", 700))

    def build_prompts(self, system_prompt: str, channel: dict, msgs: list[Msg]) -> tuple[str, str]:
        bot = self.cfg.bot_name
        channel = {**channel, "weather_location": self.cfg.value("tools.weather_location")}
        system = (system_prompt.replace("{bot_name}", bot) + "\n\n"
                  + runtime_rules(bot, channel, max_searches=int(self.cfg.value("tools.max_searches"))))
        user = (f"Channel {channel['name']} thread, oldest first. The newest message is marked >>>.\n\n"
                + format_thread(msgs, bot)
                + "\n\nShould you respond to the newest message? Answer with the JSON object only.")
        return system, user

    async def decide(self, system_prompt: str, channel: dict, msgs: list[Msg]) -> Decision:
        channel = {**channel, "_ha_defs": await self.hass.defs_for(channel)}
        return await self._decide_raw(*self.build_prompts(system_prompt, channel, msgs), channel=channel)

    async def models(self) -> list[str]:
        llm = self.cfg.section("llm")
        key = self.cfg.llm_api_key
        r = await self.client.get(str(llm["base_url"]).rstrip("/") + "/models", timeout=10,
                                  headers={"Authorization": f"Bearer {key}"} if key else {})
        r.raise_for_status()
        return sorted(m.get("id") for m in r.json().get("data", []) if m.get("id"))

    # ---------- web search (LiteLLM MCP REST) ----------
    def _proxy_base(self) -> str:
        base = str(self.cfg.value("llm.base_url")).rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    async def _server_id(self, tool: str, headers: dict) -> str:
        """LiteLLM wants the MCP server_id with each call; look it up by tool name (cached)."""
        cache = self.__dict__.setdefault("_server_ids", {})
        if tool not in cache:
            r = await self.client.get(self._proxy_base() + "/mcp-rest/tools/list", headers=headers, timeout=15)
            r.raise_for_status()
            for t in r.json().get("tools") or []:
                sid = (t.get("mcp_info") or {}).get("server_id")
                if sid:
                    cache.setdefault(t.get("name"), sid)
            if tool not in cache:
                names = ", ".join(sorted(k for k in cache)) or "none"
                raise RuntimeError(f"LiteLLM has no MCP tool named {tool!r} (available: {names})")
        return cache[tool]

    async def run_tool(self, name: str, args: dict, channel: dict | None = None) -> tuple[str, dict]:
        tcfg = self.cfg.section("tools")
        if name.startswith("ha_"):
            return await self.hass.call(name, args, channel)
        if name in T.MESH_TOOLS:
            if self.meshtools is None:
                raise T.ToolError("mesh tools are not available")
            return await self.meshtools.run(name, args, channel)
        if name == "web_search":
            q = str(args.get("query", "")).strip()
            if not q:
                raise T.ToolError("empty query")
            text, n = await self.search(q)
            return text, {"results": n}
        if name == "read_page":
            text, meta = await T.fetch_page(str(args.get("url", "")), str(args.get("look_for", "")),
                                            int(tcfg["page_max_chars"]))
            return text, meta
        if name == "weather":
            place = str(args.get("location") or "").strip() or str(tcfg["weather_location"] or "").strip()
            if not place:
                raise T.ToolError("no location given and no default weather location is set")
            return await T.weather(self.client, place, str(tcfg["weather_units"]))
        raise T.ToolError(f"unknown tool {name}")

    async def search(self, query: str) -> tuple[str, int]:
        """Run the configured MCP search tool through LiteLLM. Returns (compact text for the model, result count)."""
        tools = self.cfg.section("tools")
        key = self.cfg.llm_api_key
        args = {"query": query[:400], "count": int(tools["search_results"])}
        if tools["search_country"]:
            args["country"] = str(tools["search_country"]).upper()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        sid = await self._server_id(tools["search_tool"], headers)
        r = await self.client.post(self._proxy_base() + "/mcp-rest/tools/call", headers=headers,
                                   json={"name": tools["search_tool"], "arguments": args, "server_id": sid}, timeout=30)
        if r.status_code >= 400:
            self.__dict__.get("_server_ids", {}).pop(tools["search_tool"], None)  # server may have been re-created
            raise RuntimeError(f"LiteLLM search HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("isError"):
            raise RuntimeError(str(data.get("content"))[:200])
        lines = []
        for item in data.get("content") or []:
            text = item.get("text") or ""
            try:
                objs = json.loads(text)
                objs = objs if isinstance(objs, list) else [objs]
            except (json.JSONDecodeError, TypeError):
                objs = [{"description": text}]
            for o in objs:
                if not isinstance(o, dict):
                    continue
                desc = re.sub(r"<[^>]+>", "", " ".join([o.get("description") or ""] + (o.get("extra_snippets") or [])[:1]))
                desc = re.sub(r"\s+", " ", desc).strip()[:350]
                lines.append(f"{len(lines) + 1}. {o.get('title', '')} - {desc} ({o.get('url', '')})")
        return ("\n".join(lines)[:3500] or "No results."), len(lines)

    async def _decide_raw(self, system: str, user: str, channel: dict | None = None,
                          use_tools: bool = True) -> Decision:
        """Try the channel's primary model, then its fallback. Service failures of the primary (unreachable,
        HTTP error, timeout) hold it down for failover_hold_s so later messages go straight to the fallback."""
        t0 = time.monotonic()
        llm = self.cfg.section("llm")
        chain = self.model_chain(channel)
        has_fb = len(chain) > 1
        attempts: list[dict] = []
        d = None
        for i, model in enumerate(chain):
            is_primary_with_fb = i == 0 and has_fb
            if is_primary_with_fb and self.held(model):
                attempts.append({"model": model, "skipped": "held after recent failure: "
                                 + self.health[model].get("last_error", "")[:150]})
                continue
            sys_txt = system
            if i > 0 and any(a.get("skipped") or a.get("kind") == "service" for a in attempts):
                sys_txt += OFFLINE_NOTE
            timeout = float(llm["timeout_s"])
            if is_primary_with_fb:
                timeout = min(timeout, float(llm["primary_timeout_s"]))
            d = await self._decide_once(sys_txt, user, channel if use_tools else None, model, timeout)
            if d.error is None:
                self.mark_ok(model)
                break
            hold = float(llm["failover_hold_s"]) if is_primary_with_fb and d.error_kind == "service" else 0
            self.mark_fail(model, d.error, hold)
            attempts.append({"model": model, "error": d.error[:200], "kind": d.error_kind})
            if i + 1 < len(chain):
                log.warning("model %s failed (%s), falling back to %s", model, d.error[:120], chain[i + 1])
        d.attempts = attempts
        d.fallback = d.model != chain[0]
        d.latency_s = round(time.monotonic() - t0, 2)
        return d

    async def _decide_once(self, system: str, user: str, channel: dict | None, model: str,
                           timeout: float) -> Decision:
        t0 = time.monotonic()
        d = Decision(system=system, user=user, model=model)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        ha_defs = (channel or {}).get("_ha_defs") or []
        names = (T.enabled_tools(channel) + [x["function"]["name"] for x in ha_defs]) if channel else []
        max_searches = int(self.cfg.value("tools.max_searches")) if names else 0
        tools = (T.tool_defs([n for n in names if n in T.DEFS]) + ha_defs) if names else None

        def add_usage(u):
            for k, v in (u or {}).items():
                if isinstance(v, (int, float)):
                    d.usage[k] = d.usage.get(k, 0) + v
        try:
            content = None
            while True:
                budget_left = len(d.searches) < max_searches
                if tools and budget_left:
                    content, u, calls = await self._chat(messages, DECISION_SCHEMA, tools, "auto", model, timeout)
                elif tools:  # searches used up: answer now, JSON enforced, no more tool calls
                    content, u, calls = await self._chat(messages, DECISION_SCHEMA, tools, "none", model, timeout)
                else:
                    content, u, calls = await self._chat(messages, DECISION_SCHEMA, model=model, timeout=timeout)
                add_usage(u)
                if not calls or not budget_left:
                    break
                messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
                for call in calls:
                    fn = call.get("function") or {}
                    name, args = fn.get("name", ""), T.parse_args(fn.get("arguments"))
                    ts = time.monotonic()
                    rec = {"tool": name, "query": T.summarize_args(name, args)}
                    if name not in names or len(d.searches) >= max_searches:
                        result = "That tool isn't available for this call."
                        rec["error"] = "skipped"
                    else:
                        try:
                            result, meta = await self.run_tool(name, args, channel)
                            rec.update(meta)
                            log.info("tool %s(%r) ok", name, rec["query"])
                        except Exception as e:
                            msg = str(e) if isinstance(e, T.ToolError) else f"{type(e).__name__}: {e}"
                            result, rec["error"] = f"{name} failed: {msg}", msg[:200]
                            log.warning("tool %s(%r) failed: %s", name, rec["query"], msg)
                    rec["ms"] = round((time.monotonic() - ts) * 1000)
                    d.searches.append(rec)
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            d.raw = content or ""
            try:
                obj = parse_json(d.raw)
            except (json.JSONDecodeError, ValueError):
                if not tools:
                    raise
                # Free-form answer from a tool round: ask once more with JSON enforced.
                messages.append({"role": "assistant", "content": d.raw})
                messages.append({"role": "user", "content": "Now give your decision as the JSON object only."})
                content, u, _ = await self._chat(messages, DECISION_SCHEMA, tools, "none", model, timeout)
                add_usage(u)
                d.raw = content
                obj = parse_json(content)
            d.respond = bool(obj.get("respond"))
            d.reason = str(obj.get("reason", ""))[:300]
            parts = obj.get("parts") or []
            if isinstance(parts, str):
                parts = [parts]
            d.parts = [str(p) for p in parts if str(p).strip()]
            if d.respond and not d.parts:
                d.respond, d.reason = False, (d.reason + " [no text]").strip()
        except Exception as e:  # network, HTTP, JSON: never reply on a failed decision
            d.error = (str(e) if isinstance(e, (TokenLimit, httpx.HTTPStatusError))
                       else f"{type(e).__name__}: {e}")[:400] or type(e).__name__
            content_err = isinstance(e, (TokenLimit, ValueError, KeyError, IndexError, TypeError)) \
                and not isinstance(e, httpx.HTTPError)
            d.error_kind = "content" if content_err else "service"
            log.warning("%s decision failed (%s): %s", model, d.error_kind, d.error)
        d.latency_s = round(time.monotonic() - t0, 2)
        return d

    async def shorten(self, parts: list[str], channel: dict) -> list[str] | None:
        """Ask the model to compress an over-long answer to fit. Returns new parts or None."""
        bot = self.cfg.bot_name
        maxp = int(channel["max_parts"])
        per = part_budget(int(channel["packet_text_bytes"]), bot, maxp)
        system = ("You compress answers for a tiny-bandwidth mesh radio. Plain ASCII, no markdown, telegraphic, "
                  "keep the key facts and numbers, drop everything else. "
                  'Return ONLY JSON: {"respond": true, "reason": "shortened", "parts": ["..."]}. '
                  f"At most {maxp} parts, each at most {per} characters. Prefer fewer parts.")
        d = await self._decide_raw(system, "Compress this answer:\n\n" + " ".join(parts), channel, use_tools=False)
        return d.parts if d.parts and not d.error else None
