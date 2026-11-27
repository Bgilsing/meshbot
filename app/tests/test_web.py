"""Web API tests: real aiohttp app, fake LLM and radio."""
import asyncio
import base64
import json

import yaml
from aiohttp.test_utils import TestClient, TestServer

from app import main as M
from app import web as W
from app.brain import Decision
from app.config import Config

from .test_pipeline import CFG, FakeBrain, FakeMesh

PW = "correct horse"


def setup(tmp_path, monkeypatch):
    (tmp_path / "conf" / "prompts").mkdir(parents=True)
    (tmp_path / "conf" / "config.yaml").write_text("# top comment kept\n" + yaml.safe_dump(CFG))
    (tmp_path / "conf" / "prompts" / "system.md").write_text("You are {bot_name}.")
    monkeypatch.setattr(M, "DATA", tmp_path / "data")
    monkeypatch.setattr(M, "DECISIONS", tmp_path / "data" / "decisions.jsonl")
    monkeypatch.setattr(W, "INITIAL_PW", tmp_path / "data" / "initial_password.txt")
    monkeypatch.setenv("WEB_PASSWORD", PW)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    bot = M.Bot(Config(tmp_path / "conf" / "config.yaml", data_dir=tmp_path / "data"))
    bot.brain = FakeBrain(lambda m: Decision(respond=True, reason="q", parts=["Answer " + m[-1].text]))
    bot.brain.ping = None
    bot.mesh = FakeMesh()
    bot.mesh.channel_status, bot.mesh.companion_name, bot.mesh.last_error, bot.mesh.sent_count = {}, "TestBot", None, 0
    return bot


def auth(pw=PW):
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def with_client(tmp_path, monkeypatch, fn):
    bot = setup(tmp_path, monkeypatch)

    async def go():
        async with TestClient(TestServer(W.make_app(bot))) as c:
            await fn(c, bot)
    asyncio.run(go())
    return bot


def test_auth_required(tmp_path, monkeypatch):
    async def fn(c, bot):
        assert (await c.get("/api/status")).status == 401
        assert (await c.get("/api/status", headers=auth("nope"))).status == 401
        r = await c.get("/api/status", headers=auth())
        assert r.status == 200 and (await r.json())["bot_name"] == "TestBot"
        assert (await c.get("/", headers=auth())).status == 200
        assert (await c.get("/static/app.js", headers=auth())).status == 200
    with_client(tmp_path, monkeypatch, fn)


def test_settings_roundtrip_and_validation(tmp_path, monkeypatch):
    async def fn(c, bot):
        r = await c.post("/api/settings", headers=auth(), json={"values": {"llm.model": "other", "defaults.cooldown_s": 45}})
        assert r.status == 200
        assert bot.cfg.value("llm.model") == "other" and bot.cfg.channels()[0]["cooldown_s"] == 45
        r = await c.post("/api/settings", headers=auth(), json={"values": {"defaults.max_parts": 99}})
        assert r.status == 400 and "at most" in (await r.json())["error"]
        r = await c.post("/api/settings", headers=auth(), json={"values": {"nope": 1}})
        assert r.status == 400
    with_client(tmp_path, monkeypatch, fn)
    assert (tmp_path / "conf" / "config.yaml").read_text().startswith("# top comment kept")


def test_channels_and_secrets_never_leak(tmp_path, monkeypatch):
    async def fn(c, bot):
        chans = [{"name": "#testbot", "slot": 1, "mode": "live"}, {"name": "Private", "slot": 3, "eagerness": "high"}]
        r = await c.put("/api/channels", headers=auth(), json={"channels": chans})
        assert r.status == 200
        r = await c.post("/api/channels/secret", headers=auth(), json={"name": "Private", "secret": "zz"})
        assert r.status == 400
        r = await c.post("/api/channels/secret", headers=auth(), json={"name": "Private", "secret": "generate"})
        gen = (await r.json())["generated"]
        assert len(gen) == 32 and bot.cfg.channel_secret(bot.cfg.channel_by_name("Private")) == bytes.fromhex(gen)
        await c.post("/api/secrets", headers=auth(), json={"llm_api_key": "sk-secret-123"})
        assert bot.cfg.llm_api_key == "sk-secret-123"
        body = await (await c.get("/api/settings", headers=auth())).text()
        assert gen not in body and "sk-secret-123" not in body
        s = json.loads(body)
        assert s["secrets"]["llm_api_key"] is True
        assert [c["secret_set"] for c in s["channels"]] == [False, True]
        dup = [{"name": "#a", "slot": 1}, {"name": "#b", "slot": 1}]
        assert (await c.put("/api/channels", headers=auth(), json={"channels": dup})).status == 400
    bot = with_client(tmp_path, monkeypatch, fn)
    assert oct((tmp_path / "data" / "secrets.json").stat().st_mode)[-3:] == "600"
    assert "sk-secret" not in (tmp_path / "conf" / "config.yaml").read_text()


def test_prompts_and_raw_config(tmp_path, monkeypatch):
    async def fn(c, bot):
        assert (await c.put("/api/prompts", headers=auth(), json={"which": "#testbot", "text": "Be brief."})).status == 200
        p = await (await c.get("/api/prompts", headers=auth())).json()
        assert p["channels"]["#testbot"] == "Be brief.\n"
        assert (await c.put("/api/prompts", headers=auth(), json={"which": "system", "text": " "})).status == 400
        r = await c.put("/api/raw_config", headers=auth(), json={"text": "bot_name: X\nchannels: [{name: '#a', slot: 99}]"})
        assert r.status == 400
        assert bot.cfg.bot_name == "TestBot"
    with_client(tmp_path, monkeypatch, fn)


def test_playground_drafts_and_manual_send(tmp_path, monkeypatch):
    async def fn(c, bot):
        r = await c.post("/api/playground", headers=auth(), json={
            "channel": "#testbot", "thread": [{"sender": "Al", "text": "hi?"}], "system_draft": "DRAFT"})
        d = await r.json()
        assert d["respond"] and d["parts"] == ["Answer hi?"] and d["bytes"] == [len("TestBot: Answer hi?")]
        assert bot.mesh.sent == []  # playground never transmits
        r = await c.post("/api/send", headers=auth(), json={"channel": "#testbot", "text": "x" * 300})
        assert r.status == 400
        r = await c.post("/api/send", headers=auth(), json={"channel": "#testbot", "text": "hello mesh"})
        assert r.status == 200 and bot.mesh.sent == [(1, "hello mesh")]
    with_client(tmp_path, monkeypatch, fn)


def test_password_change(tmp_path, monkeypatch):
    async def fn(c, bot):
        r = await c.post("/api/password", headers=auth(), json={"current": "wrong", "new": "newpassword1"})
        assert r.status == 400
        r = await c.post("/api/password", headers=auth(), json={"current": PW, "new": "newpassword1"})
        assert r.status == 200
        assert (await c.get("/api/status", headers=auth())).status == 401
        assert (await c.get("/api/status", headers=auth("newpassword1"))).status == 200
    with_client(tmp_path, monkeypatch, fn)


def test_generated_password_when_none_set(tmp_path, monkeypatch):
    bot = setup(tmp_path, monkeypatch)
    monkeypatch.delenv("WEB_PASSWORD")
    W.ensure_password(bot.cfg)
    pw = (tmp_path / "data" / "initial_password.txt").read_text().strip()
    assert W.password_ok(bot.cfg, pw) and not W.password_ok(bot.cfg, "x")


def test_channel_join_qr(tmp_path, monkeypatch):
    import hashlib

    async def fn(c, bot):
        chans = [{"name": "#testbot", "slot": 1}, {"name": "Home Base", "slot": 3}]
        assert (await c.put("/api/channels", headers=auth(), json={"channels": chans})).status == 200
        # private channel without a secret yet: no QR
        assert (await c.post("/api/channels/qr", headers=auth(), json={"name": "Home Base"})).status == 400
        r = await (await c.post("/api/channels/secret", headers=auth(),
                                json={"name": "Home Base", "secret": "generate"})).json()
        assert r["url"] == f"meshcore://channel/add?name=Home%20Base&secret={r['generated']}"
        assert r["svg"].startswith("<svg") and "#282727" in r["svg"]
        q = await (await c.post("/api/channels/qr", headers=auth(), json={"name": "Home Base"})).json()
        assert q["url"] == r["url"]
        h = await (await c.post("/api/channels/qr", headers=auth(), json={"name": "#testbot"})).json()
        key = hashlib.sha256(b"#testbot").hexdigest()[:32]
        assert h["url"] == f"meshcore://channel/add?name=%23testbot&secret={key}"
        assert (await c.post("/api/channels/qr", json={"name": "#testbot"})).status == 401
        assert (await c.post("/api/channels/qr", headers=auth(), json={"name": "nope"})).status in (400, 404)
    with_client(tmp_path, monkeypatch, fn)
