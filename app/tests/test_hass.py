import asyncio
import json

import httpx
import pytest
import yaml

from app import tools as T
from app.brain import Brain, tool_rules
from app.config import Config
from app.hass import blocked_call, safe_name, short_desc, tool_status, trim_schema
from app.history import Msg

from .test_pipeline import CFG

HA_TOOLS = [
    {"name": "ha_restart", "description": "Restart Home Assistant", "inputSchema": {"type": "object",
                                                                                     "properties": {}}},
    {"name": "ha_call_service", "description": "Call any service.\n\nLong details " + "y" * 3000,
     "inputSchema": {"type": "object", "properties": {"domain": {"type": "string", "description": "d" * 500},
                                                      "service": {"type": "string"},
                                                      "entity_id": {"type": "string"}}}},
    {"name": "GetLiveContext", "description": "Current state of all exposed entities",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "HassTurnOn", "description": "Turns on a device",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "area": {"type": "string"}}}},
    {"name": "HassLockUnlock", "description": "Unlocks a lock", "inputSchema": {"type": "object", "properties": {}}},
]
SERVERS = [{"server_id": "B1", "server_name": "brave_search", "alias": "brave_search"},
           {"server_id": "H1", "server_name": "home_assistant", "alias": "homeassistant"}]


def make(tmp_path, ha=None, servers=SERVERS, channel=None, replies=None):
    p = tmp_path / "config.yaml"
    chans = [{"name": "Home", "slot": 2, **(channel or {})}]
    p.write_text(yaml.safe_dump({**CFG, "channels": chans, "home_assistant": ha or {}}))
    cfg = Config(p, data_dir=tmp_path / "data")
    cfg.secrets.set_channel("Home", "00112233445566778899aabbccddeeff")
    calls = []

    def handler(req):
        body = json.loads(req.content) if req.content else {}
        calls.append((req.url.path, dict(req.url.params), body))
        if req.url.path == "/v1/mcp/server":
            return httpx.Response(200, json=servers)
        if req.url.path == "/mcp-rest/tools/list":
            assert req.url.params["server_id"] == "H1"
            return httpx.Response(200, json={"tools": HA_TOOLS})
        if req.url.path == "/mcp-rest/tools/call":
            assert body["server_id"] == "H1"
            text = {"GetLiveContext": "Kitchen light: off. Porch temp: 41F. " + "x" * 50,
                    "HassTurnOn": "Turned on kitchen light"}[body["name"]]
            return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})
        n = sum(1 for c in calls if c[0].endswith("/chat/completions"))
        return replies(n, body)
    b = Brain(cfg)
    b.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return b, cfg, calls


def run(c):
    return asyncio.run(c)


def test_classification_helpers():
    R, C, B = ["ha_get_state", "GetLiveContext"], ["ha_call_service", "Hass*"], ["unlock", "disarm"]
    assert tool_status("ha_get_state", R, C, B) == "read" and tool_status("getlivecontext", R, C, B) == "read"
    assert tool_status("HassTurnOn", R, C, B) == "control" and tool_status("ha_call_service", R, C, B) == "control"
    assert tool_status("HassLockUnlock", R, C, B) == "blocked"
    assert tool_status("ha_restart", R, C, B) == "hidden" and tool_status("ha_get_state_x", R, C, B) == "hidden"
    assert safe_name("Hass Turn.On") == "ha_Hass_Turn_On" and safe_name("ha_get_state") == "ha_get_state"
    assert short_desc("First para\nwraps.\n\nSecond") == "First para wraps."
    assert trim_schema({"description": "x" * 500, "p": [{"description": "y" * 300}]}, 10) == {
        "description": "x" * 10, "p": [{"description": "y" * 10}]}


def test_blocked_service_calls():
    blocked = ["lock.*", "alarm_control_panel.*", "homeassistant.restart"]
    assert blocked_call({"domain": "lock", "service": "unlock", "entity_id": "lock.front"}, blocked) == "lock.unlock"
    assert blocked_call({"domain": "light", "service": "turn_on", "entity_id": "lock.front"}, blocked) == "lock.front"
    assert blocked_call({"domain": "homeassistant", "service": "restart"}, blocked) == "homeassistant.restart"
    assert blocked_call({"service": "homeassistant.restart"}, blocked) == "homeassistant.restart"
    assert blocked_call({"domain": "light", "service": "turn_on", "entity_id": "light.a, light.b"}, blocked) is None
    assert blocked_call({"operations": [{"entity_id": "light.k", "action": "on"},
                                        {"entity_id": "lock.back", "action": "unlock"}]}, blocked) == "lock.unlock"
    assert blocked_call({"ws_command": "config/auth/delete"}, blocked) == "raw websocket commands"


def test_off_by_default_no_lookup(tmp_path):
    b, cfg, calls = make(tmp_path)
    ch = cfg.channels()[0]
    assert ch["home_assistant"] == "off"
    assert run(b.hass.defs_for(ch)) == [] and calls == []


def test_read_mode_offers_only_lookups(tmp_path):
    b, cfg, _ = make(tmp_path, channel={"home_assistant": "read"})
    names = [d["function"]["name"] for d in run(b.hass.defs_for(cfg.channels()[0]))]
    assert names == ["ha_GetLiveContext"]  # call_service, restart, HassTurnOn not offered in read mode


def test_control_mode_defaults_offer_every_device_tool(tmp_path):
    b, cfg, _ = make(tmp_path, channel={"home_assistant": "control"})
    names = [d["function"]["name"] for d in run(b.hass.defs_for(cfg.channels()[0]))]
    assert names == ["ha_call_service", "ha_GetLiveContext", "ha_HassTurnOn", "ha_HassLockUnlock"]  # no blocks
    assert "ha_restart" not in names  # admin tools are still not on any list


def test_hashtag_channels_never_get_access(tmp_path):
    b, cfg, calls = make(tmp_path)
    ch = {"name": "#public", "home_assistant": "control"}
    assert run(b.hass.defs_for(ch)) == [] and calls == []
    with pytest.raises(T.ToolError, match="isn't allowed"):
        run(b.hass.call("ha_HassTurnOn", {}, ch))


def test_control_mode_blocks_sensitive(tmp_path):
    b, cfg, _ = make(tmp_path, ha={"blocked_tools": ["unlock"]}, channel={"home_assistant": "control"})
    defs = run(b.hass.defs_for(cfg.channels()[0]))
    names = [d["function"]["name"] for d in defs]
    assert names == ["ha_call_service", "ha_GetLiveContext", "ha_HassTurnOn"]  # restart hidden, unlock blocked
    assert defs[2]["function"]["parameters"]["properties"]["area"] == {"type": "string"}
    assert defs[0]["function"]["description"] == "[Home Assistant] Call any service."
    assert len(defs[0]["function"]["parameters"]["properties"]["domain"]["description"]) == 160


def test_control_refuses_blocked_service(tmp_path):
    b, cfg, calls = make(tmp_path, ha={"blocked_services": ["lock.*"]}, channel={"home_assistant": "control"})
    with pytest.raises(T.ToolError, match="lock.unlock is blocked"):
        run(b.hass.call("ha_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
                        cfg.channels()[0]))
    assert not any(c[0] == "/mcp-rest/tools/call" for c in calls)


def test_call_rechecks_permission(tmp_path):
    b, cfg, calls = make(tmp_path, channel={"home_assistant": "read"})
    ch = cfg.channels()[0]
    with pytest.raises(T.ToolError, match="isn't allowed"):
        run(b.hass.call("ha_HassTurnOn", {"name": "kitchen"}, ch))
    with pytest.raises(T.ToolError, match="isn't allowed"):
        run(b.hass.call("ha_restart", {}, {**ch, "home_assistant": "control"}))
    assert not any(c[0] == "/mcp-rest/tools/call" for c in calls)


def test_call_result_truncated(tmp_path):
    b, cfg, _ = make(tmp_path, ha={"max_result_chars": 500}, channel={"home_assistant": "read"})
    text, meta = run(b.hass.call("ha_GetLiveContext", {}, cfg.channels()[0]))
    assert text.startswith("Kitchen light: off") and meta == {"ha_tool": "GetLiveContext", "action": False}


def test_missing_server_explains(tmp_path):
    b, cfg, _ = make(tmp_path, servers=SERVERS[:1], channel={"home_assistant": "read"})
    r = run(b.hass.overview())
    assert not r["ok"] and "no Home Assistant MCP server" in r["error"] and "brave_search" in r["error"]
    assert run(b.hass.defs_for(cfg.channels()[0])) == [] and "Grant the key" in b.hass.last_error


def test_named_server(tmp_path):
    b, _, _ = make(tmp_path, ha={"mcp_server": "homeassistant", "blocked_tools": ["unlock"]})
    r = run(b.hass.overview())
    assert r["ok"] and r["server_id"] == "H1"
    assert {t["name"]: t["status"] for t in r["tools"]} == {
        "ha_restart": "hidden", "ha_call_service": "control", "GetLiveContext": "read", "HassTurnOn": "control",
        "HassLockUnlock": "blocked"}


def test_decide_controls_a_device(tmp_path):
    def replies(n, body):
        names = [t["function"]["name"] for t in body.get("tools") or []]
        if n == 1:
            assert names[-4:] == ["ha_call_service", "ha_GetLiveContext", "ha_HassTurnOn", "ha_HassLockUnlock"]
            sysmsg = body["messages"][0]["content"]
            assert "everyone on it is authorized" in sysmsg and "Never ask for confirmation" in sysmsg
            msg = {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "ha_HassTurnOn", "arguments": json.dumps({"name": "kitchen light"})}}]}
            return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "tool_calls"}]})
        assert "Turned on kitchen light" in body["messages"][-1]["content"]
        reply = '{"respond": true, "reason": "asked", "parts": ["Kitchen light is on."]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})
    b, cfg, calls = make(tmp_path, channel={"home_assistant": "control"}, replies=replies)
    ch = cfg.channels()[0]
    d = run(b.decide("You are {bot_name}.", ch, [Msg(1, "Home", 0, "Alex", "turn on the kitchen light", False)]))
    assert d.error is None and d.parts == ["Kitchen light is on."]
    assert d.searches[0]["tool"] == "ha_HassTurnOn" and d.searches[0]["action"] is True
    assert d.searches[0]["query"] == 'HassTurnOn {"name":"kitchen light"}'


def test_rules_read_only_wording():
    ch = {"home_assistant": "read", "_ha_defs": [{"function": {"name": "ha_GetLiveContext"}}]}
    r = tool_rules(ch, 4)
    assert "ha_GetLiveContext" in r and "can't change anything" in r
    assert tool_rules({"home_assistant": "read"}, 4) == ""
