"""End-to-end pipeline with a fake LLM and a fake radio (no network)."""
import asyncio
import json

import yaml

from app import main as M
from app.brain import Decision
from app.compress import blen
from app.config import Config
from app.mesh import Incoming

CFG = {
    "bot_name": "TestBot", "enabled": True,
    "llm": {"model": "x"},
    "defaults": {"mode": "live", "eagerness": "normal", "max_parts": 3, "packet_text_bytes": 140, "debounce_s": 0.05,
                 "part_gap_s": 0.01, "cooldown_s": 0, "max_replies_per_hour": 20, "per_sender_per_hour": 2,
                 "history": 30, "history_max_age_h": 12},
    "ignore_senders": ["Porchlight"],
    "channels": [{"name": "#testbot", "slot": 1}, {"name": "#quiet", "slot": 2, "mode": "shadow"}],
}


class FakeBrain:
    def __init__(self, reply=None, delay=0.0):
        self.calls, self.reply, self.delay, self.shortened = [], reply, delay, 0
        self.health = {}

    def model_chain(self, ch):
        return ["fake-model"]

    async def decide(self, prompt, ch, msgs):
        self.calls.append([m.text for m in msgs])
        await asyncio.sleep(self.delay)
        return self.reply(msgs) if self.reply else Decision(respond=False, reason="chatter")

    async def shorten(self, parts, ch):
        self.shortened += 1
        return ["Short version."]


class FakeMesh:
    connected = True

    def __init__(self):
        self.sent = []

    async def send(self, slot, text):
        self.sent.append((slot, text))
        return True


def make_bot(tmp_path, monkeypatch, brain, cfg=CFG):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("You are {bot_name}.")
    monkeypatch.setattr(M, "DATA", tmp_path / "data")
    monkeypatch.setattr(M, "DECISIONS", tmp_path / "data" / "decisions.jsonl")
    monkeypatch.setattr(M, "load_prompt", lambda *a: "You are {bot_name}.")
    bot = M.Bot(Config(p, data_dir=tmp_path / "data"))
    bot.brain, bot.mesh = brain, FakeMesh()
    return bot


def msg(text, sender="Alice", slot=1, live=True, ts=None):
    return Incoming(slot=slot, sender=sender, text=text, sender_ts=ts, snr=5.0, hops=2, live=live)


def decisions(tmp_path):
    return [json.loads(x) for x in (tmp_path / "data" / "decisions.jsonl").read_text().splitlines()]


def run(coro):
    return asyncio.run(coro)


def test_silent_decision_sends_nothing(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain())

    async def go():
        await bot.on_message(msg("hey all"))
        await asyncio.sleep(0.3)
    run(go())
    assert bot.mesh.sent == []
    assert decisions(tmp_path)[0]["outcome"] == "silent"


def test_burst_is_debounced_into_one_call(tmp_path, monkeypatch):
    brain = FakeBrain()
    bot = make_bot(tmp_path, monkeypatch, brain)

    async def go():
        for t in ("one", "two", "three"):
            await bot.on_message(msg(t))
        await asyncio.sleep(0.3)
    run(go())
    assert brain.calls == [["one", "two", "three"]]


def test_live_reply_multi_part_and_history(tmp_path, monkeypatch):
    long = " ".join(f"Fact {i} about radio waves here." for i in range(10))
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=[long])))

    async def go():
        await bot.on_message(msg("how does lora work?"))
        await asyncio.sleep(0.4)
    run(go())
    texts = [t for _, t in bot.mesh.sent]
    assert 1 < len(texts) <= 3 and texts[0].endswith(f"1/{len(texts)}")
    assert all(blen("TestBot: " + t) <= 140 for t in texts)
    own = [m for m in bot.history.recent("#testbot") if m.own]
    assert [m.text for m in own] == texts
    assert decisions(tmp_path)[0]["outcome"] == "sent"


def test_overflow_triggers_shorten(tmp_path, monkeypatch):
    """With 'more' paging off, an over-long answer gets one shorten re-ask."""
    brain = FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["word " * 500]))
    bot = make_bot(tmp_path, monkeypatch, brain, {**CFG, "defaults": {**CFG["defaults"], "more_pages": 0}})

    async def go():
        await bot.on_message(msg("explain everything"))
        await asyncio.sleep(0.3)
    run(go())
    assert brain.shortened == 1
    assert [t for _, t in bot.mesh.sent] == ["Short version."]


def test_overflow_is_held_for_more_when_paging_is_on(tmp_path, monkeypatch):
    brain = FakeBrain(lambda m: Decision(respond=True, reason="q", parts=[f"Fact {i} here." for i in range(60)]))
    bot = make_bot(tmp_path, monkeypatch, brain)

    async def go():
        await bot.on_message(msg("explain everything"))
        await asyncio.sleep(0.3)
    run(go())
    sent = [t for _, t in bot.mesh.sent]
    assert brain.shortened == 0 and len(sent) == 3 and sent[0].startswith("Fact 0 here.")
    assert sent[-1].endswith("(reply more)") and bot.pages["#testbot"]["pages"][1]


def test_shadow_channel_never_transmits(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["hi"])))

    async def go():
        await bot.on_message(msg("question?", slot=2))
        await asyncio.sleep(0.3)
    run(go())
    assert bot.mesh.sent == []
    d = decisions(tmp_path)[0]
    assert d["outcome"] == "shadow" and d["parts"] == ["hi"]


def test_kill_switch(tmp_path, monkeypatch):
    cfg = {**CFG, "enabled": False}
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["hi"])), cfg)

    async def go():
        await bot.on_message(msg("question?"))
        await asyncio.sleep(0.3)
    run(go())
    assert bot.mesh.sent == []


def test_ignored_sender_backlog_and_unknown_slot(tmp_path, monkeypatch):
    brain = FakeBrain()
    bot = make_bot(tmp_path, monkeypatch, brain)

    async def go():
        await bot.on_message(msg("porch light on", sender="Porchlight"))
        await bot.on_message(msg("old msg", live=False))
        await bot.on_message(msg("other chan", slot=7))
        await asyncio.sleep(0.3)
    run(go())
    assert brain.calls == []
    assert [m.text for m in bot.history.recent("#testbot")] == ["porch light on", "old msg"]


def test_duplicate_packets_stored_once(tmp_path, monkeypatch):
    brain = FakeBrain()
    bot = make_bot(tmp_path, monkeypatch, brain)

    async def go():
        await bot.on_message(msg("dup", ts=1000))
        await bot.on_message(msg("dup", ts=1000))
        await asyncio.sleep(0.3)
    run(go())
    assert len(bot.history.recent("#testbot")) == 1


def test_per_sender_cap(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch, FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["ok"])))

    async def go():
        for i in range(3):
            await bot.on_message(msg(f"q{i}?"))
            await asyncio.sleep(0.25)
    run(go())
    assert len(bot.mesh.sent) == 2
    assert decisions(tmp_path)[-1]["outcome"] == "blocked:sender_hourly_cap"


def test_message_during_thinking_supersedes(tmp_path, monkeypatch):
    brain = FakeBrain(lambda m: Decision(respond=True, reason="q", parts=[f"re {m[-1].text}"]), delay=0.2)
    bot = make_bot(tmp_path, monkeypatch, brain)

    async def go():
        await bot.on_message(msg("first?"))
        await asyncio.sleep(0.1)   # LLM call in flight
        await bot.on_message(msg("actually second?"))
        await asyncio.sleep(0.8)
    run(go())
    assert [t for _, t in bot.mesh.sent] == ["re actually second?"]
    assert [d["outcome"] for d in decisions(tmp_path)] == ["superseded", "sent"]
