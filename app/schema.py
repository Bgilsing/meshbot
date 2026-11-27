"""Every tunable setting, described once. The web UI renders its forms from this, and config validation and
defaults come from it too. Adding a control later is one F(...) line here (plus reading the value in code)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class F:
    key: str                 # key within its section (or within a channel)
    type: str                # int | float | bool | str | text | choice | list | secret
    label: str
    default: Any = None
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: list | None = None
    placeholder: str = ""
    inherit_from: str | None = None  # per-channel-only field: blank inherits this setting (e.g. llm.model)


# Settings that live under `defaults:` and can be overridden per channel.
CHANNEL_FIELDS = [
    F("model", "model", "Primary model", None,
      "Blank = the primary model in Settings > Model.", inherit_from="llm.model"),
    F("fallback_model", "model", "Fallback model", None,
      "Used when the primary fails or times out. Blank = Settings > Model; 'none' = no fallback.",
      inherit_from="llm.fallback_model"),
    F("mode", "choice", "Mode", "shadow", "live = transmit. shadow = decide and log, never transmit. off = ignore.",
      choices=["live", "shadow", "off"]),
    F("eagerness", "choice", "Eagerness", "normal", "How readily the model jumps into the conversation.",
      choices=["low", "normal", "high"]),
    F("max_parts", "int", "Max parts per reply", 3, "Most packets one answer may use.", 1, 6),
    F("packet_text_bytes", "int", "Packet size (bytes)", 140,
      "Bytes for \"Name: text\" in total. The bot name and the 1/3 marker come out of this.", 60, 160),
    F("debounce_s", "float", "Debounce (s)", 4, "Wait this long after the last message before thinking.", 0, 120),
    F("part_gap_s", "float", "Gap between parts (s)", 8, "Lets repeaters finish relaying one part before the next.",
      0, 120),
    F("cooldown_s", "float", "Cooldown (s)", 20, "Minimum time between replies on one channel.", 0, 3600),
    F("max_replies_per_hour", "int", "Max replies / hour", 20, "Per channel.", 0, 1000),
    F("per_sender_per_hour", "int", "Max replies / sender / hour", 6, "Replies one person can trigger per hour.",
      0, 1000),
    F("history", "int", "Context messages", 30, "Recent messages sent to the model.", 1, 200),
    F("history_max_age_h", "float", "Context max age (h)", 12, "Older messages are left out of the context.",
      0.1, 720),
    F("web_search", "bool", "Web search + read pages", False,
      "Let the model search the web (LiteLLM's Brave MCP tool) and read the pages it finds. Slower replies."),
    F("weather", "bool", "Weather tool", True, "Let the model look up current weather, forecast and NWS alerts."),
    F("callsign_lookup", "bool", "Callsign lookup", True,
      "Look up amateur callsigns: first name, last initial and city/state only. Uses QRZ when its login is set in "
      "Secrets, otherwise free US FCC data (callook.info)."),
    F("mesh_data", "bool", "Mesh data tools", False,
      "Let the model look up nodes, hops, network activity (CoreScope) and our repeater's stats (openHop). "
      "Read-only, nothing is transmitted."),
    F("home_assistant", "choice", "Home Assistant", "off",
      "off = no access. read = look up states and sensors only. control = may switch any device. "
      "Private channels only: hashtag channels never get access, whatever this says.",
      choices=["off", "read", "control"]),
    F("radio_queries", "bool", "Radio queries", False,
      "Also let it transmit: log in to a named repeater as guest for its neighbors/status, and run live path "
      "discovery. Uses airtime; capped in Settings > Mesh data. Needs Mesh data tools on."),
]

SECTIONS = [
    {"id": "general", "title": "General", "prefix": "", "fields": [
        F("enabled", "bool", "Transmit enabled", True,
          "Kill switch. Off = the bot keeps deciding and logging but never transmits."),
        F("bot_name", "str", "Bot name", "MeshBot",
          "Must match the openHop companion's node name. Use 'Rename companion' on the dashboard after changing it."),
        F("ignore_senders", "list", "Ignore senders", [],
          "Never reply to these (other bots). One per line, case-insensitive."),
        F("heartbeat_url", "str", "Uptime Kuma push URL", "", "Optional. Pinged every 60 s while connected.",
          placeholder="http://uptime-kuma:3001/api/push/..."),
    ]},
    {"id": "llm", "title": "Model (LiteLLM)", "prefix": "llm", "fields": [
        F("base_url", "str", "Base URL", "http://localhost:4000/v1",
          "OpenAI-compatible endpoint (LiteLLM recommended: web search and Home Assistant use its MCP servers), "
          "ending in /v1."),
        F("model", "model", "Primary model", "",
          "Default for every channel (channels can override). Model name as LiteLLM knows it."),
        F("fallback_model", "model", "Fallback model", "",
          "Used when the primary errors or times out, e.g. a local model for when the internet is down. "
          "Blank = no fallback."),
        F("primary_timeout_s", "float", "Primary timeout (s)", 20,
          "How long to wait for the primary before switching to the fallback. Short = fast failover. "
          "Only applies when a fallback is set.", 3, 600),
        F("failover_hold_s", "float", "Failover hold (s)", 300,
          "After the primary fails, go straight to the fallback for this long, then try the primary again.", 0, 86400),
        F("temperature", "float", "Temperature", 0.4, "Lower = more consistent, higher = more creative.", 0, 2),
        F("max_tokens", "int", "Max tokens", 700, "Upper bound on the model's output when thinking is off.", 50, 8000),
        F("thinking_max_tokens", "int", "Max tokens (thinking on)", 4000,
          "Used instead of Max tokens when thinking is on: the reasoning counts against the limit too. "
          "Raise Timeout as well if replies start timing out.", 200, 32000),
        F("timeout_s", "float", "Timeout (s)", 60, "Give up on a model call after this long.", 5, 600),
        F("thinking", "bool", "Thinking (reasoning)", False, "Qwen3-style reasoning: slower, sometimes smarter."),
        F("response_format", "choice", "Response format", "json_schema",
          "How JSON output is requested. Falls back automatically if the backend rejects it.",
          choices=["json_schema", "json_object", "none"]),
    ]},
    {"id": "tools", "title": "Tools (search, pages, weather)", "prefix": "tools", "fields": [
        F("search_tool", "str", "MCP tool", "brave_web_search",
          "Tool name as LiteLLM lists it (/mcp-rest/tools/list). Turn search on per channel or in Channel defaults."),
        F("max_searches", "int", "Max tool calls per reply", 4,
          "Searches + page reads + weather lookups for one reply. Each call adds a few seconds.", 1, 8),
        F("search_results", "int", "Results per search", 5, "How many results the model gets back.", 1, 10),
        F("search_country", "str", "Country", "US", "Two-letter country code for results (ALL for none)."),
        F("page_max_chars", "int", "Page text limit (chars)", 5000,
          "How much of a page the model gets. Long pages are trimmed to the parts matching what it's looking for.",
          1000, 20000),
        F("weather_location", "str", "Default weather location", "",
          "Used when someone asks about the weather without saying where. City, 'City, ST' or ZIP."),
        F("weather_units", "choice", "Weather units", "imperial", "", choices=["imperial", "metric"]),
    ]},
    {"id": "mesh", "title": "Mesh data (CoreScope, openHop, radio queries)", "prefix": "mesh", "fields": [
        F("corescope_url", "str", "CoreScope URL", "http://127.0.0.1:8090", "CoreScope (MeshCore network analyzer) API; no auth."),
        F("openhop_url", "str", "openHop URL", "http://127.0.0.1:8000",
          "openHop's web API. Needs the openHop API key in Secrets; only read-only endpoints are called."),
        F("cache_s", "float", "Cache (s)", 60, "Reuse fetched map data this long.", 0, 3600),
        F("online_minutes", "float", "Observer online window (min)", 15,
          "An observer counts as online if it was seen this recently.", 1, 1440),
        F("radio_max_per_hour", "int", "Radio queries / hour", 6,
          "Most transmitting queries (guest logins, path discoveries) per hour, all channels.", 0, 60),
        F("radio_timeout_s", "float", "Radio query timeout (s)", 30, "How long to wait for a reply over the air.",
          5, 120),
        F("radio_cache_s", "float", "Radio result cache (s)", 600,
          "Repeat questions about the same repeater reuse the last answer instead of transmitting again.", 0, 86400),
        F("default_guest_password", "str", "Default guest password", "",
          "Tried when a repeater has no password of its own in Secrets > Repeater guest passwords. Usually blank."),
    ]},
    {"id": "home_assistant", "title": "Home Assistant (LiteLLM MCP)", "prefix": "home_assistant", "fields": [
        F("mcp_server", "str", "MCP server name", "",
          "The Home Assistant server's name in LiteLLM. Blank = find one named like home assistant / hass. The "
          "LiteLLM API key must be allowed to use it. Turn it on per channel (read or control)."),
        F("read_tools", "list", "Read tools",
          ["ha_get_state", "ha_search", "ha_get_overview", "ha_get_history", "ha_list_floors_areas", "ha_get_todo",
           "ha_get_zone", "ha_config_get_calendar_events", "GetLiveContext", "GetDateTime", "todo_get_items"],
          "Lookups offered in read and control mode. One tool name per line (patterns like ha_get_* allowed). "
          "Tools on no list (admin: restart, backups, config editing, deletes...) are never offered."),
        F("control_tools", "list", "Control tools",
          ["ha_bulk_control", "ha_call_service", "ha_set_todo_item", "Hass*"],
          "Extra tools offered in control mode only. Hass* = Home Assistant's built-in Assist tools."),
        F("blocked_tools", "list", "Blocked tools", [],
          "Never offered, even if on a list above. A word matched anywhere in the name, or a pattern."),
        F("blocked_services", "list", "Blocked services", [],
          "Optional: service calls to refuse even in control mode, as domain.service (patterns allowed, e.g. "
          "lock.unlock or alarm_control_panel.*). Also matched against target entity IDs. Empty = any device."),
        F("max_result_chars", "int", "Result size limit (chars)", 4000,
          "Longer tool results (e.g. a full house state dump) are cut to this.", 500, 20000),
        F("cache_s", "float", "Tool list cache (s)", 300, "How long to reuse the server's tool list.", 0, 86400),
    ]},
    {"id": "companion", "title": "openHop companion", "prefix": "companion", "fields": [
        F("host", "str", "Host", "127.0.0.1", "Changing this reconnects."),
        F("port", "int", "Port", 5070, "Changing this reconnects.", 1, 65535),
        F("path_hash_bytes", "choice", "Path hash size (bytes)", "1",
          "Bytes per repeater hash in the paths of packets the bot sends; match your network (1, 2 or 3). "
          "Applied to the companion on every connect; changing it reconnects.", choices=["1", "2", "3"]),
    ]},
    {"id": "defaults", "title": "Channel defaults", "prefix": "defaults",
     "fields": [f for f in CHANNEL_FIELDS if not f.inherit_from]},
]

CHANNEL_KEYS = tuple(f.key for f in CHANNEL_FIELDS)


def field_index() -> dict[str, F]:
    """'llm.model' -> F, 'enabled' -> F, 'defaults.mode' -> F."""
    out = {}
    for s in SECTIONS:
        for f in s["fields"]:
            out[(s["prefix"] + "." if s["prefix"] else "") + f.key] = f
    return out


def channel_field(key: str) -> F | None:
    return next((f for f in CHANNEL_FIELDS if f.key == key), None)


def coerce(f: F, value: Any) -> Any:
    """Convert a value from the UI (or YAML) to the field's type, checking bounds. Raises ValueError."""
    t = f.type
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t in ("int", "float"):
        if isinstance(value, str) and not value.strip():
            raise ValueError(f"{f.label}: a number is required")
        try:
            v = int(value) if t == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{f.label}: not a number")
        if t == "int" and isinstance(value, float) and value != int(value):
            raise ValueError(f"{f.label}: must be a whole number")
        if f.min is not None and v < f.min:
            raise ValueError(f"{f.label}: must be at least {f.min:g}")
        if f.max is not None and v > f.max:
            raise ValueError(f"{f.label}: must be at most {f.max:g}")
        return v
    if t == "choice":
        if value not in f.choices:
            raise ValueError(f"{f.label}: must be one of {', '.join(f.choices)}")
        return value
    if t == "list":
        if isinstance(value, str):
            value = value.splitlines()
        return [str(x).strip() for x in value or [] if str(x).strip()]
    return "" if value is None else str(value).strip() if t in ("str", "model") else str(value)


def as_json() -> dict:
    return {"sections": [{**s, "fields": [asdict(f) for f in s["fields"]]} for s in SECTIONS],
            "channel_fields": [asdict(f) for f in CHANNEL_FIELDS]}
