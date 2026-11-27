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
