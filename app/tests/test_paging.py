"""Long answers sent a page at a time, with "more" for the next page (fake LLM and radio, no network)."""
import asyncio

from app import main as M
from app.brain import Decision, runtime_rules
from app.compress import MORE_HINT, blen, paginate
from app.mesh import Incoming

from .test_direct import make_bot
from .test_pipeline import CFG, FakeBrain, decisions

ITEMS = [f"Repeater-{i:02d} SNR {i % 12}.5 {i}m ago" for i in range(40)]
LIVE = {"defaults": {**CFG["defaults"], "per_sender_per_hour": 50, "max_replies_per_hour": 50}}


def msg(text, sender="Alice"):
    return Incoming(slot=1, sender=sender, text=text, sender_ts=None, snr=4.0, hops=1, live=True)


def test_paginate_numbers_over_the_whole_answer():
    batches = paginate(["Repeaters heard today:", "; ".join(ITEMS)], "TestBot", 140, 3, 10)
    parts = [p for b in batches for p in b]
    total = len(parts)
    assert len(batches) > 1 and all(len(b) == 3 for b in batches[:-1]) and 1 <= len(batches[-1]) <= 3
    for i, p in enumerate(parts, 1):  # 1/8 ... 8/8, the hint only on a batch's last packet
        assert p.removesuffix(MORE_HINT).endswith(f" {i}/{total}")
        assert blen(f"TestBot: {p}") <= 140
    assert all(b[-1].endswith(MORE_HINT) for b in batches[:-1]) and not batches[-1][-1].endswith(MORE_HINT)
    assert not any(p.endswith(MORE_HINT) for b in batches for p in b[:-1])
    text = " ".join(parts)
    assert all(item in text for item in ITEMS)  # nothing dropped


def test_paginate_keeps_list_items_whole():
    names = [f"Node {i} Solar 1W V2" for i in range(30)]
    for b in paginate([", ".join(names)], "TestBot", 140, 3, 10):
        for part in b:
            body = part.removesuffix(MORE_HINT).rsplit(" ", 1)[0]  # drop the " 2/8" marker
            assert body.endswith(",") or body.endswith("V2"), part


def test_paginate_keeps_separator_between_model_pieces():
    batches = paginate(["Heard: Alpha 1W; Beta R2", "Gamma X; Delta Y", "Eps Z"]
                       + [f"N{i} Q; M{i} W" for i in range(30)], "TestBot", 140, 3, 10)
    text = " ".join(part for b in batches for part in b)
    assert "Beta R2; Gamma X" in text and "Delta Y; Eps Z" in text


def test_paginate_short_answer_is_one_packet():
    assert paginate(["Just this."], "TestBot", 140, 3, 10) == [["Just this."]]


def test_paginate_cuts_at_max_batches():
    batches = paginate(["; ".join(ITEMS * 3)], "TestBot", 140, 2, 3)
    assert len(batches) == 3 and batches[-1][-1].endswith("6/6") and "..." in batches[-1][-1]


def test_more_request():
    assert M.more_request("more", "TestBot") == 0
    assert M.more_request("  More please!", "TestBot") == 0
    assert M.more_request("@TestBot next", "TestBot") == 0
    assert M.more_request("TestBot: page 3", "TestBot") == 3
    assert M.more_request("tell me more about lora", "TestBot") is None
    assert M.more_request("no more", "TestBot") is None


def test_more_sends_next_page_without_the_model(tmp_path, monkeypatch):
    brain = FakeBrain(lambda m: Decision(respond=True, reason="list", parts=["Repeaters heard today:"],
                                         more=["; ".join(ITEMS)]))
    bot = make_bot(tmp_path, monkeypatch, brain, LIVE)

    async def go():
        await bot.on_message(msg("list all repeaters heard today"))
        await asyncio.sleep(0.4)
        first = list(bot.mesh.sent)
        await bot.on_message(msg("more", sender="Bob"))  # anyone on the channel may ask
        await asyncio.sleep(0.3)
        return first
    first = asyncio.run(go())
    assert len(brain.calls) == 1  # "more" never reached the model
    assert len(first) == 3 and first[-1][1].endswith(MORE_HINT) and first[0][1].endswith(" 1/" + first[0][1].split("/")[-1])
    page2 = bot.mesh.sent[len(first):]
    assert page2 and page2[0][1].rsplit(" ", 1)[-1].startswith("4/")
    recs = decisions(tmp_path)
    assert recs[0]["later_pages"] and recs[1]["reason"].startswith("batch 2/") and recs[1]["outcome"] == "sent"


def test_more_without_held_pages_goes_to_the_model(tmp_path, monkeypatch):
    brain = FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["ok"]))
    bot = make_bot(tmp_path, monkeypatch, brain, LIVE)

    async def go():
        await bot.on_message(msg("more"))
        await asyncio.sleep(0.3)
    asyncio.run(go())
    assert len(brain.calls) == 1 and bot.mesh.sent == [(1, "ok")]


def test_paging_off_shortens_instead(tmp_path, monkeypatch):
    brain = FakeBrain(lambda m: Decision(respond=True, reason="list", parts=["Repeaters:"], more=["; ".join(ITEMS)]))
    bot = make_bot(tmp_path, monkeypatch, brain,
                   {"defaults": {**LIVE["defaults"], "more_pages": 0}})

    async def go():
        await bot.on_message(msg("list all repeaters"))
        await asyncio.sleep(0.3)
    asyncio.run(go())
    assert brain.shortened == 1 and bot.pages == {}


def test_rules_mention_more():
    ch = {"name": "#x", "packet_text_bytes": 140, "max_parts": 3, "more_pages": 5}
    assert "reply 'more'" in runtime_rules("TestBot", ch)
    assert "more is always []" in runtime_rules("TestBot", {**ch, "more_pages": 0})
