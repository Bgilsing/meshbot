"""Direct messages and adverts, with a fake LLM and a fake radio (no network)."""
import asyncio
import json
import time

import httpx
import pytest
import yaml

from app import main as M
from app import tools as T
from app.brain import Brain, Decision, runtime_rules
from app.compress import blen, fit, part_budget
from app.config import Config, prompt_slug
from app.history import Msg
from app.mesh import Incoming, Mesh

from .test_pipeline import CFG, FakeBrain, decisions


class FakeMesh:
    connected = True

    def __init__(self, ack=True):
        self.sent, self.dms, self.adverts, self.ack = [], [], [], ack

    async def send(self, slot, text):
        self.sent.append((slot, text))
        return True

    async def send_dm(self, key, text):
        self.dms.append((key, text))
        return self.ack

    async def send_advert(self, flood):
        self.adverts.append(flood)


def make_bot(tmp_path, monkeypatch, brain, extra=None):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**CFG, **(extra or {})}))
    monkeypatch.setattr(M, "DATA", tmp_path / "data")
    monkeypatch.setattr(M, "DECISIONS", tmp_path / "data" / "decisions.jsonl")
    monkeypatch.setattr(M, "STATE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(M, "load_prompt", lambda *a: "You are {bot_name}.")
    bot = M.Bot(Config(p, data_dir=tmp_path / "data"))
    bot.brain, bot.mesh = brain, FakeMesh()
    return bot


def dm(text, sender="Alice", key="a1b2c3d4e5f6", live=True):
    return Incoming(slot=-1, sender=sender, text=text, sender_ts=None, snr=4.0, hops=1, live=live, dm=True,
                    sender_key=key)


def run(coro):
    return asyncio.run(coro)


DM_LIVE = {"direct": {"mode": "live", "debounce_s": 0.05, "part_gap_s": 0.01, "cooldown_s": 0}}


def test_budget_without_prefix():
    assert part_budget(150, "MeshBot", 1, prefix=False) == 150
    assert part_budget(150, "MeshBot", 1) == 150 - len("MeshBot: ")
    f = fit(["x" * 150], "MeshBot", 150, 1, prefix=False)
    assert f.parts == ["x" * 150] and not f.overflow


def test_dm_profile_and_names(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**CFG, "direct": {"mode": "live", "web_search": True}}))
    cfg = Config(p, data_dir=tmp_path / "data")
    prof = cfg.dm_profile("Alice")
    assert prof["name"] == "@Alice" and prof["dm"] and prof["mode"] == "live" and prof["web_search"]
    assert prof["home_assistant"] == "off" and prof["model"] is None and prof["packet_text_bytes"] == 150
    assert prompt_slug("@Alice") == prompt_slug("@Bob") == "direct-messages"
    p.write_text(yaml.safe_dump({**CFG, "channels": [{"name": "@sneaky", "slot": 1}]}))
    with pytest.raises(Exception):
        Config(p, data_dir=tmp_path / "d2")


def test_dm_reply_goes_to_sender_key(tmp_path, monkeypatch):
    long = " ".join(f"word{i}" for i in range(50))
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=[long])),
                   DM_LIVE)

    async def go():
        await bot.on_message(dm("how does lora work?"))
        await asyncio.sleep(0.4)
    run(go())
    assert bot.mesh.sent == []  # never on a channel
    keys = {k for k, _ in bot.mesh.dms}
    assert keys == {"a1b2c3d4e5f6"} and len(bot.mesh.dms) >= 2
    assert all(blen(t) <= 150 for _, t in bot.mesh.dms)
    assert blen(bot.mesh.dms[0][1]) > 150 - len("TestBot: ") - 5  # the name prefix isn't reserved
    hist = bot.history.recent("@Alice")
    assert hist[0].text == "how does lora work?" and [m.text for m in hist if m.own] == [t for _, t in bot.mesh.dms]
    rec = decisions(tmp_path)[0]
    assert rec["channel"] == "@Alice" and rec["outcome"] == "sent"
    assert bot.history.dm_conversations() == ["@Alice"]


def test_dm_shadow_by_default_and_ignored(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["hi"])),
                   {"direct": {"debounce_s": 0.05}})

    async def go():
        await bot.on_message(dm("hello"))
        await bot.on_message(dm("hey", sender="Porchlight", key="ffffffffffff"))
        await bot.on_message(dm("old", sender="Bob", key="bbbbbbbbbbbb", live=False))
        await asyncio.sleep(0.3)
    run(go())
    assert bot.mesh.dms == [] and [d["outcome"] for d in decisions(tmp_path)] == ["shadow"]


def test_dm_caps(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["ok"])),
                   {"direct": {**DM_LIVE["direct"], "per_sender_per_hour": 1}})

    async def go():
        for t in ("one", "two"):
            await bot.on_message(dm(t))
            await asyncio.sleep(0.25)
    run(go())
    assert len(bot.mesh.dms) == 1
    assert [d["outcome"] for d in decisions(tmp_path)] == ["sent", "blocked:sender_hourly_cap"]


def test_unacked_dm_is_reported(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["ok"])),
                   DM_LIVE)
    bot.mesh.ack = False

    async def go():
        await bot.on_message(dm("ping"))
        await asyncio.sleep(0.25)
    run(go())
    assert decisions(tmp_path)[0]["outcome"] == "send_failed_after_0"


def test_mesh_parses_direct_message():
    got = []

    async def on_msg(m):
        got.append(m)

    class MC:
        is_connected = True

        def get_contact_by_key_prefix(self, prefix):
            return {"adv_name": "Alice T-Deck"} if prefix == "a1b2c3d4e5f6" else None

    class Ev:
        def __init__(self, p):
            self.payload = p

    cfg = type("C", (), {"bot_name": "MeshBot"})()
    mesh = Mesh(cfg, on_msg)
    mesh.mc, mesh.connected_at = MC(), 0
    run(mesh._on_direct_msg(Ev({"pubkey_prefix": "a1b2c3d4e5f6", "path_len": 255, "txt_type": 0, "text": " hi ",
                                "sender_timestamp": 1, "SNR": 6.0})))
    run(mesh._on_direct_msg(Ev({"pubkey_prefix": "a1b2c3d4e5f6", "path_len": 2, "txt_type": 1, "text": "cli"})))
    assert len(got) == 1
    m = got[0]
    assert (m.dm, m.sender, m.sender_key, m.text, m.hops, m.live) == (True, "Alice T-Deck", "a1b2c3d4e5f6", "hi",
                                                                        None, True)


# ---------- brain ----------
def test_dm_prompt_wording():
    ch = {"name": "@Alice", "dm": True, "sender": "Alice", "packet_text_bytes": 150, "max_parts": 2,
          "advert_requests": True}
    r = runtime_rules("MeshBot", ch, max_searches=4)
    assert "Private direct message conversation with Alice" in r and "Always respond" in r
    assert "at most 150 characters" in r and "send_advert" in r


def test_dm_never_offers_home_assistant(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**CFG, "direct": {"home_assistant": "control"}}))
    cfg = Config(p, data_dir=tmp_path / "data")
    b = Brain(cfg)
    called = []
    b.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: called.append(r) or httpx.Response(500)))
    prof = cfg.dm_profile("Alice")
    assert prof["home_assistant"] == "off"
    assert run(b.hass.defs_for({**prof, "home_assistant": "control"})) == [] and called == []


def test_dm_silence_is_reasked(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(CFG))
    cfg = Config(p, data_dir=tmp_path / "data")
    b = Brain(cfg)
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            out = '{"respond": false, "reason": "just thanks", "parts": []}'
        else:
            out = '{"respond": true, "reason": "dm", "parts": ["You are welcome!"]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": out}, "finish_reason": "stop"}]})
    b.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prof = {**cfg.dm_profile("Alice"), "weather": False, "callsign_lookup": False, "advert_requests": False}
    d = run(b.decide("You are {bot_name}.", prof, [Msg(1, "@Alice", time.time(), "Alice", "thanks!", False)]))
    assert d.respond and d.parts == ["You are welcome!"] and len(bodies) == 2
    assert "you must reply" in bodies[1]["messages"][1]["content"]
    assert "Direct messages between you and Alice" in bodies[0]["messages"][1]["content"]


# ---------- adverts ----------
def test_advert_request_rate_limit_and_state(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain())
    ch = bot.cfg.channel_by_name("#testbot")
    text, meta = run(bot.request_advert(ch))
    assert text == "Advert sent." and meta["action"] and bot.mesh.adverts == [True]
    with pytest.raises(T.ToolError, match="min ago"):
        run(bot.request_advert(ch))
    st = json.loads((tmp_path / "data" / "state.json").read_text())
    assert st["last_advert_source"] == "request #testbot" and st["last_advert"] > 0
    run(bot.advertise("dashboard"))  # the button ignores the rate limit
    assert bot.mesh.adverts == [True, True]


def test_advert_request_dry_run_and_kill_switch(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain())
    shadow = bot.cfg.channel_by_name("#quiet")
    text, meta = run(bot.request_advert(shadow))
    assert meta.get("dry_run") and bot.mesh.adverts == []
    text, meta = run(bot.request_advert({**bot.cfg.channel_by_name("#testbot"), "_dry_run": True}))
    assert meta.get("dry_run") and bot.mesh.adverts == []
    bot.cfg.set_values({"enabled": False})
    with pytest.raises(T.ToolError, match="switched off"):
        run(bot.request_advert(bot.cfg.channel_by_name("#testbot")))
    assert bot.mesh.adverts == []


def test_scheduled_advert(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(), {"advert": {"interval_h": 48, "flood": False}})
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) > 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(M.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(bot.advert_loop())
    assert bot.mesh.adverts == [False]  # first pass sends (no state yet), later passes don't
    # a restart remembers it
    bot2 = make_bot(tmp_path, monkeypatch, FakeBrain(), {"advert": {"interval_h": 48}})
    sleeps.clear()
    with pytest.raises(asyncio.CancelledError):
        run(bot2.advert_loop())
    assert bot2.mesh.adverts == []
    # overdue -> sends again
    bot2.state["last_advert"] = time.time() - 49 * 3600
    sleeps.clear()
    with pytest.raises(asyncio.CancelledError):
        run(bot2.advert_loop())
    assert bot2.mesh.adverts == [True]


def test_tool_offered_per_channel():
    assert "send_advert" in T.enabled_tools({"advert_requests": True})
    assert "send_advert" not in T.enabled_tools({"advert_requests": False})
