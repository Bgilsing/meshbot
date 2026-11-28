import asyncio
from types import SimpleNamespace

import yaml

from app.config import Config
from app.mesh import Mesh
from meshcore import EventType

from .test_pipeline import CFG


class FakeCommands:
    def __init__(self, mode, refuse=False):
        self.mode, self.refuse, self.sets = mode, refuse, []

    async def get_path_hash_mode(self):
        return self.mode

    async def set_path_hash_mode(self, mode):
        self.sets.append(mode)
        if self.refuse:
            return SimpleNamespace(type=EventType.ERROR, payload="nope")
        self.mode = mode
        return SimpleNamespace(type=EventType.OK, payload={})


def mesh_with(tmp_path, bytes_, cmds):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({**CFG, "companion": {"path_hash_bytes": bytes_}}))
    m = Mesh(Config(p, data_dir=tmp_path / "data"), on_message=None)
    m.mc = SimpleNamespace(commands=cmds)
    return m


def test_path_hash_applied_when_different(tmp_path):
    cmds = FakeCommands(mode=0)
    m = mesh_with(tmp_path, "2", cmds)
    asyncio.run(m._apply_path_hash())
    assert cmds.sets == [1] and m.path_hash_bytes == 2


def test_path_hash_left_alone_when_matching(tmp_path):
    cmds = FakeCommands(mode=2)
    m = mesh_with(tmp_path, "3", cmds)
    asyncio.run(m._apply_path_hash())
    assert cmds.sets == [] and m.path_hash_bytes == 3


def test_path_hash_refused_is_reported(tmp_path):
    m = mesh_with(tmp_path, "2", FakeCommands(mode=0, refuse=True))
    asyncio.run(m._apply_path_hash())
    assert m.path_hash_bytes is None


def test_path_hash_change_triggers_reconnect(tmp_path):
    a = mesh_with(tmp_path, "1", FakeCommands(0)).cfg.link_signature()
    b = mesh_with(tmp_path, "2", FakeCommands(0)).cfg.link_signature()
    assert a != b


def test_bootstrap_creates_starter_files_once(tmp_path):
    from app.config import bootstrap, validate
    made = bootstrap(tmp_path)
    assert made == ["config.yaml", "prompts/system.md"]
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    validate(data)
    assert data["channels"][0]["name"] == "#meshbot" and data["defaults"]["mode"] == "shadow"
    (tmp_path / "config.yaml").write_text("bot_name: Mine\n")
    assert bootstrap(tmp_path) == [] and "Mine" in (tmp_path / "config.yaml").read_text()


def test_login_retries_each_with_fresh_flood(tmp_path, monkeypatch):
    calls = []
    pk = "ab" * 32
    answered = asyncio.Event()

    class Cmds:
        async def reset_path(self, key):
            calls.append("reset")
            return SimpleNamespace(type=EventType.OK, payload={})

        async def send_login(self, key, pw):
            calls.append("login")
            if calls.count("login") == 2:  # the repeater only hears the second attempt
                asyncio.get_running_loop().call_later(0.05, answered.set)
            return SimpleNamespace(type=EventType.MSG_SENT, payload={"suggested_timeout": 800})

    class Disp:
        async def wait_for_event(self, et, attribute_filters=None, timeout=None):
            if et == EventType.LOGIN_SUCCESS:
                await answered.wait()
                return SimpleNamespace(payload={"is_admin": False})
            await asyncio.sleep(60)

    import app.mesh as mesh_mod
    monkeypatch.setattr(mesh_mod.random, "uniform", lambda a, b: 0)
    m = mesh_with(tmp_path, "1", Cmds())
    m.mc = SimpleNamespace(commands=Cmds(), dispatcher=Disp(), is_connected=True)
    # 3 attempts in 3 s: each gets at least ~1 s, the first goes unanswered
    import time as _t
    t0 = _t.monotonic()
    assert asyncio.run(m.login(pk, "", 3, attempts=3)) == {"is_admin": False}
    assert calls == ["reset", "login", "reset", "login"] and _t.monotonic() - t0 < 3


def test_login_gives_up_after_attempts(tmp_path, monkeypatch):
    import app.mesh as mesh_mod
    monkeypatch.setattr(mesh_mod.random, "uniform", lambda a, b: 0)
    calls = []

    class Cmds:
        async def reset_path(self, key):
            calls.append("reset")
            return SimpleNamespace(type=EventType.OK, payload={})

        async def send_login(self, key, pw):
            calls.append("login")
            return SimpleNamespace(type=EventType.MSG_SENT, payload={"suggested_timeout": 0})

    class Disp:
        async def wait_for_event(self, et, attribute_filters=None, timeout=None):
            await asyncio.sleep(timeout)
            return None

    m = mesh_with(tmp_path, "1", Cmds())
    m.mc = SimpleNamespace(commands=Cmds(), dispatcher=Disp(), is_connected=True)
    import pytest
    with pytest.raises(TimeoutError, match="after 2 tries"):
        asyncio.run(m.login("cd" * 32, "", 2.5, attempts=2))
    assert calls == ["reset", "login", "reset", "login"]


def test_requests_retry_and_flood_on_last_try(tmp_path, monkeypatch):
    import app.mesh as mesh_mod
    monkeypatch.setattr(mesh_mod.random, "uniform", lambda a, b: 0)
    calls = []

    class Cmds:
        async def reset_path(self, key):
            calls.append("reset")
            return SimpleNamespace(type=EventType.OK, payload={})

        async def fetch_all_neighbours(self, key, pubkey_prefix_length=4, timeout=0, min_timeout=0):
            calls.append("req")
            if calls.count("req") == 1:
                raise asyncio.TimeoutError()
            if calls.count("req") == 2:
                return None  # dropped
            return {"neighbours_count": 1, "neighbours": [{"pubkey": "aa", "snr": 5}]}

        async def req_status_sync(self, key, timeout=0, min_timeout=0):
            calls.append("status")
            return None

    m = mesh_with(tmp_path, "1", Cmds())
    m.mc = SimpleNamespace(commands=Cmds(), is_connected=True)
    r = asyncio.run(m.neighbours("ab" * 32, 3, attempts=3))
    assert r["neighbours_count"] == 1
    assert calls == ["req", "req", "reset", "req"]  # third (last) try floods
    calls.clear()
    assert asyncio.run(m.status_req("ab" * 32, 2, attempts=2)) is None
    assert calls == ["status", "reset", "status"]


def test_stray_login_failed_does_not_end_login(tmp_path, monkeypatch):
    """A late duplicate of an earlier reply shows up as LOGIN_FAILED right after we send; the real LOGIN_SUCCESS
    comes a few seconds later. That must count as logged in, not 'refused'."""
    import app.mesh as mesh_mod
    monkeypatch.setattr(mesh_mod.random, "uniform", lambda a, b: 0)
    calls = []

    class Cmds:
        async def reset_path(self, key):
            calls.append("reset")
            return SimpleNamespace(type=EventType.OK, payload={})

        async def send_login(self, key, pw):
            calls.append("login")
            return SimpleNamespace(type=EventType.MSG_SENT, payload={"suggested_timeout": 0})

    class Disp:
        async def wait_for_event(self, et, attribute_filters=None, timeout=None):
            if et == EventType.LOGIN_FAILED:
                await asyncio.sleep(0.05)
                return SimpleNamespace(payload={})
            await asyncio.sleep(0.4)
            return SimpleNamespace(payload={"is_admin": False})

    m = mesh_with(tmp_path, "1", Cmds())
    m.mc = SimpleNamespace(commands=Cmds(), dispatcher=Disp(), is_connected=True)
    assert asyncio.run(m.login("ab" * 32, "", 3, attempts=3)) == {"is_admin": False}
    assert calls == ["reset", "login"]


def test_login_rejected_every_time_is_reported(tmp_path, monkeypatch):
    import app.mesh as mesh_mod
    monkeypatch.setattr(mesh_mod.random, "uniform", lambda a, b: 0)
    calls = []

    class Cmds:
        async def reset_path(self, key):
            return SimpleNamespace(type=EventType.OK, payload={})

        async def send_login(self, key, pw):
            calls.append("login")
            return SimpleNamespace(type=EventType.MSG_SENT, payload={"suggested_timeout": 0})

    class Disp:
        async def wait_for_event(self, et, attribute_filters=None, timeout=None):
            if et == EventType.LOGIN_FAILED:
                await asyncio.sleep(0.05)
                return SimpleNamespace(payload={})
            await asyncio.sleep(timeout)
            return None

    m = mesh_with(tmp_path, "1", Cmds())
    m.mc = SimpleNamespace(commands=Cmds(), dispatcher=Disp(), is_connected=True)
    import pytest
    with pytest.raises(PermissionError, match="rejected all 2 login tries"):
        asyncio.run(m.login("cd" * 32, "", 2.5, attempts=2))
    assert calls == ["login", "login"]
