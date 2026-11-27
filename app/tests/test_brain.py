import asyncio
import json

import httpx
import yaml

from app.brain import Brain
from app.config import Config
from app.history import Msg

from .test_pipeline import CFG


def make(tmp_path, llm, reply, finish="stop"):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**CFG, "llm": {**CFG["llm"], **llm}}))
    cfg = Config(p, data_dir=tmp_path / "data")
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": finish}],
                                         "usage": {"completion_tokens": 9}})
    b = Brain(cfg)
    b.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return b, cfg, seen


def decide(b, cfg):
    ch = cfg.channels()[0]
    return asyncio.run(b.decide("You are {bot_name}.", ch, [Msg(1, ch["name"], 0, "Al", "hi?", False)]))


def test_truncated_output_explains_token_limit(tmp_path):
    b, cfg, seen = make(tmp_path, {"thinking": True}, '{"respond": true,', finish="length")
    d = decide(b, cfg)
    assert not d.respond and "ran out of tokens" in d.error and "thinking_max_tokens=4000" in d.error
    assert seen["body"]["max_tokens"] == 4000


def test_thinking_off_uses_max_tokens(tmp_path):
    b, cfg, seen = make(tmp_path, {"thinking": False, "max_tokens": 321},
                        '<think>x</think>{"respond": true, "reason": "q", "parts": ["ok"]}')
    d = decide(b, cfg)
    assert d.respond and d.parts == ["ok"] and seen["body"]["max_tokens"] == 321
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_search_tool_loop(tmp_path):
    p = tmp_path / "config.yaml"
    cfg_d = {**CFG, "defaults": {**CFG["defaults"], "web_search": True}, "tools": {"max_searches": 1}}
    p.write_text(yaml.safe_dump(cfg_d))
    cfg = Config(p, data_dir=tmp_path / "data")
    calls = []

    def handler(req):
        body = json.loads(req.content) if req.content else {}
        calls.append((req.url.path, body))
        if req.url.path.endswith("/mcp-rest/tools/list"):
            return httpx.Response(200, json={"tools": [{"name": "brave_web_search", "mcp_info": {"server_id": "S1"}}]})
        if req.url.path.endswith("/mcp-rest/tools/call"):
            assert body["name"] == "brave_web_search" and body["arguments"]["query"] == "komets score"
            assert body["server_id"] == "S1"
            item = {"title": "Komets win", "description": "Komets <strong>4-2</strong>", "url": "http://x"}
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(item)}]})
        n = sum(1 for c in calls if c[0].endswith("/chat/completions"))
        if n == 1:  # first round: tool offered, no forced JSON, model searches
            assert body["tool_choice"] == "auto" and "response_format" not in body
            msg = {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "web_search", "arguments": json.dumps({"query": "komets score"})}}]}
            return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "tool_calls"}]})
        # second round: search budget used, JSON enforced, results visible
        assert body["tool_choice"] == "none" and body["response_format"]["type"] == "json_schema"
        assert "Komets 4-2" in body["messages"][-1]["content"]
        reply = '{"respond": true, "reason": "score q", "parts": ["Komets won 4-2."]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                                         "usage": {"total_tokens": 5}})
    b = Brain(cfg)
    b.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = decide(b, cfg)
    assert d.error is None and d.parts == ["Komets won 4-2."]
    assert d.searches[0]["query"] == "komets score" and d.searches[0]["results"] == 1
    assert "Tools available: web_search, read_page, weather" in d.system and d.usage["total_tokens"] == 5
    assert "read_page the 1-2 most promising" in d.system


def failover_brain(tmp_path, behaviour, llm=None, channel=None):
    """behaviour(model) -> 'down' | 'garbage' | 'ok'"""
    p = tmp_path / "config.yaml"
    chans = [{"name": "#testbot", "slot": 1, **(channel or {})}]
    p.write_text(yaml.safe_dump({**CFG, "channels": chans, "llm": {
        "model": "cloud", "fallback_model": "local", "primary_timeout_s": 5, "failover_hold_s": 300, **(llm or {})}}))
    cfg = Config(p, data_dir=tmp_path / "data")
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append((body["model"], req.extensions.get("timeout", {}).get("read"), body["messages"][0]["content"]))
        mode = behaviour(body["model"])
        if mode == "down":
            raise httpx.ConnectError("no route to host")
        text = "not json at all" if mode == "garbage" else \
            json.dumps({"respond": True, "reason": "q", "parts": [f"from {body['model']}"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
    b = Brain(cfg)
    b.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return b, cfg, calls


def test_failover_when_primary_unreachable_then_hold(tmp_path):
    b, cfg, calls = failover_brain(tmp_path, lambda m: "down" if m == "cloud" else "ok")
    d = decide(b, cfg)
    assert d.parts == ["from local"] and d.model == "local" and d.fallback
    assert d.attempts[0]["model"] == "cloud" and d.attempts[0]["kind"] == "service"
    assert calls[0][1] == 5 and calls[1][1] == 60          # short primary timeout, full one for fallback
    assert "internet may be down" in calls[1][2]            # fallback is told why
    assert b.held("cloud")
    calls.clear()
    d = decide(b, cfg)                                       # during the hold: straight to the fallback
    assert [c[0] for c in calls] == ["local"] and "skipped" in d.attempts[0] and d.fallback
    asyncio.run(b.ping("local"))
    assert b.held("cloud")                                   # pinging the fallback doesn't clear the primary


def test_ping_success_ends_hold(tmp_path):
    state = {"cloud": "down"}
    b, cfg, calls = failover_brain(tmp_path, lambda m: state.get(m, "ok"))
    decide(b, cfg)
    assert b.held("cloud")
    state["cloud"] = "ok"
    asyncio.run(b.ping("cloud"))
    assert not b.held("cloud")
    d = decide(b, cfg)
    assert d.model == "cloud" and not d.fallback


def test_bad_output_falls_back_without_hold(tmp_path):
    b, cfg, calls = failover_brain(tmp_path, lambda m: "garbage" if m == "cloud" else "ok")
    d = decide(b, cfg)
    assert d.model == "local" and d.attempts[0]["kind"] == "content"
    assert not b.held("cloud") and "internet may be down" not in calls[1][2]


def test_both_down_reports_error(tmp_path):
    b, cfg, calls = failover_brain(tmp_path, lambda m: "down")
    d = decide(b, cfg)
    assert d.error and not d.respond and len(d.attempts) == 2


def test_channel_overrides_and_none(tmp_path):
    b, cfg, _ = failover_brain(tmp_path, lambda m: "ok", channel={"model": "fast", "fallback_model": "none"})
    assert b.model_chain(cfg.channels()[0]) == ["fast"]
    b, cfg, _ = failover_brain(tmp_path, lambda m: "ok", channel={"fallback_model": "other"})
    assert b.model_chain(cfg.channels()[0]) == ["cloud", "other"]
    b, cfg, calls = failover_brain(tmp_path, lambda m: "ok", llm={"fallback_model": ""})
    assert b.model_chain(cfg.channels()[0]) == ["cloud"]
    decide(b, cfg)
    assert calls[0][1] == 60                                  # no fallback: full timeout for the primary
