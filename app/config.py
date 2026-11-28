"""Config + prompt loading (mtime hot reload) and safe writing (used by the web UI)."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable

import yaml
from ruamel.yaml import YAML

from .schema import CHANNEL_FIELDS, CHANNEL_KEYS, SECTIONS, channel_field, coerce, field_index

log = logging.getLogger("meshbot.config")

ROOT = Path(os.environ.get("MESHBOT_ROOT", "/srv"))
CONF = ROOT / "conf"
CONFIG_PATH = CONF / "config.yaml"
DATA = ROOT / "data"

_rt = YAML()
_rt.preserve_quotes = True
_rt.width = 120
_rt.indent(mapping=2, sequence=4, offset=2)

SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Secrets:
    """Secrets set from the web UI, in data/secrets.json (mode 600). Values never leave the server.

    Keys: "llm_api_key", "openhop_api_key", "guest_passwords" ("Name=password" lines), "web_password_hash",
    "channel:<channel name>" (32 hex chars).
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def has(self, key: str) -> bool:
        return bool(self._load().get(key))

    def set(self, key: str, value: str | None) -> None:
        with self._lock:
            d = self._load()
            if value is None or value == "":
                d.pop(key, None)
            else:
                d[key] = value
            atomic_write(self.path, json.dumps(d, indent=1))
            os.chmod(self.path, 0o600)

    def set_channel(self, name: str, hexkey: str | None) -> None:
        if hexkey and not re.fullmatch(r"[0-9a-fA-F]{32}", hexkey):
            raise ValueError("secret must be 32 hex characters (16 bytes)")
        self.set("channel:" + name, hexkey.lower() if hexkey else None)

    def channel(self, name: str) -> str | None:
        return self.get("channel:" + name)


class Config:
    """Holds the last good config. reload() is cheap; call it whenever you need fresh settings."""

    def __init__(self, path: Path = CONFIG_PATH, data_dir: Path | None = None):
        self.path = Path(path)
        self.prompts_dir = self.path.parent / "prompts"
        self.secrets = Secrets((data_dir or self.path.parent.parent / "data") / "secrets.json")
        self._mtime = None
        self._lock = threading.Lock()
        self.data: dict = {}
        self.last_error: str | None = None
        self.reload(force=True)

    # ---------- reading ----------
    def reload(self, force: bool = False) -> bool:
        """Re-read config.yaml if it changed. Returns True when it did. A broken file keeps the old config."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            if force:
                raise
            return False
        if not force and mtime == self._mtime:
            return False
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
            validate(data)
        except Exception as e:
            if force and not self.data:
                raise
            self.last_error = str(e)
            log.error("config.yaml invalid, keeping previous config: %s", e)
            self._mtime = mtime
            return False
        self.data, self._mtime, self.last_error = data, mtime, None
        log.info("config loaded (%d channels)", len(data.get("channels") or []))
        return True

    def get(self, k, default=None):
        return self.data.get(k, default)

    def value(self, path: str):
        """Dotted lookup with the schema default as fallback, e.g. value('llm.model')."""
        cur = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                f = field_index().get(path)
                return f.default if f else None
            cur = cur[part]
        return cur

    def section(self, prefix: str) -> dict:
        """A whole section with schema defaults filled in (e.g. 'llm')."""
        sec = next(s for s in SECTIONS if s["prefix"] == prefix)
        return {f.key: self.value(f"{prefix}.{f.key}") for f in sec["fields"]}

    @property
    def bot_name(self) -> str:
        return str(self.value("bot_name"))

    @property
    def llm_api_key(self) -> str:
        """Web UI value wins; .env LITELLM_API_KEY is the fallback."""
        return self.secrets.get("llm_api_key") or os.environ.get("LITELLM_API_KEY", "")

    @property
    def openhop_api_key(self) -> str:
        return self.secrets.get("openhop_api_key") or os.environ.get("OPENHOP_API_KEY", "")

    def guest_password(self, node_name: str) -> str | None:
        """Per-repeater guest password from the secrets store ("Name=password" lines, name case-insensitive)."""
        for line in (self.secrets.get("guest_passwords") or "").splitlines():
            name, sep, pw = line.partition("=")
            if sep and name.strip().lower() == node_name.strip().lower():
                return pw.strip()
        return None

    @property
    def enabled(self) -> bool:
        return bool(self.value("enabled"))

    def channel_defaults(self) -> dict:
        return {f.key: (None if f.inherit_from else self.value(f"defaults.{f.key}")) for f in CHANNEL_FIELDS}

    def channels(self) -> list[dict]:
        """Channel entries with defaults merged in."""
        out = []
        base = self.channel_defaults()
        for ch in self.data.get("channels") or []:
            merged = dict(base)
            merged.update({k: v for k, v in ch.items() if v is not None})
            merged["overrides"] = [k for k in CHANNEL_KEYS if ch.get(k) is not None]
            out.append(merged)
        return out

    def channel_by_slot(self, slot: int) -> dict | None:
        return next((c for c in self.channels() if int(c["slot"]) == int(slot)), None)

    def channel_by_name(self, name: str) -> dict | None:
        return next((c for c in self.channels() if c["name"].lower() == name.lower()), None)

    def dm_profile(self, sender: str) -> dict:
        """A channel-shaped settings dict for a direct-message conversation ("@<sender>")."""
        prof = self.channel_defaults()
        prof.update(self.section("direct"))
        for k in ("model", "fallback_model"):
            prof[k] = prof[k] or None
        prof.update({"name": dm_name(sender), "slot": None, "dm": True, "sender": sender, "eagerness": "high",
                     "home_assistant": "off", "overrides": []})
        return prof

    def ignored(self, sender: str) -> bool:
        return sender.lower() in {str(n).lower() for n in self.value("ignore_senders") or []}

    def channel_secret(self, ch: dict) -> bytes | None:
        """16-byte key for a private channel; None for hashtag channels. Raises if a private channel has none."""
        if str(ch["name"]).startswith("#"):
            return None
        raw = ""
        if ch.get("secret_env"):
            raw = os.environ.get(ch["secret_env"], "").strip()
        raw = raw or (self.secrets.channel(ch["name"]) or "")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
            raise ValueError(f"private channel {ch['name']} has no valid secret (set it in the web UI)")
        return bytes.fromhex(raw)

    def link_signature(self):
        """What needs a companion reconnect when it changes."""
        chans = []
        for c in self.channels():
            try:
                key = self.channel_secret(c)
            except ValueError:
                key = b"missing"
            chans.append((c["name"], int(c["slot"]), hashlib.sha256(key or b"").hexdigest()[:8]))
        comp = self.section("companion")
        return (comp["host"], comp["port"], str(comp.get("path_hash_bytes")), tuple(chans))

    # ---------- writing (web UI) ----------
    def raw_text(self) -> str:
        return self.path.read_text()

    def write_raw(self, text: str) -> None:
        data = yaml.safe_load(text) or {}
        validate(data)
        with self._lock:
            atomic_write(self.path, text)
        self.reload(force=True)

    def update(self, mutate: Callable[[dict], None]) -> None:
        """Apply mutate() to the round-trip YAML document (comments kept), validate, write, reload."""
        with self._lock:
            doc = _rt.load(self.path.read_text()) or {}
            mutate(doc)
            buf = io.StringIO()
            _rt.dump(doc, buf)
            text = buf.getvalue()
            validate(yaml.safe_load(text) or {})
            atomic_write(self.path, text)
        self.reload(force=True)

    def set_values(self, values: dict) -> None:
        """{'llm.model': 'x', 'defaults.mode': 'live', 'enabled': False}. None removes the key (back to default)."""
        idx = field_index()
        clean = {}
        for path, v in values.items():
            f = idx.get(path)
            if not f:
                raise ValueError(f"unknown setting {path}")
            clean[path] = None if v is None else coerce(f, v)

        def mutate(doc):
            for path, v in clean.items():
                parts = path.split(".")
                cur = doc
                for p in parts[:-1]:
                    if p not in cur or cur[p] is None:
                        cur[p] = {}
                    cur = cur[p]
                if v is None:
                    cur.pop(parts[-1], None)
                else:
                    cur[parts[-1]] = v
        self.update(mutate)

    def set_channels(self, channels: list[dict]) -> None:
        """Replace the channel list. Per-channel keys left None/'' inherit from defaults."""
        out = []
        for ch in channels:
            name = str(ch.get("name", "")).strip()
            entry = {"name": name, "slot": int(ch.get("slot"))}
            if ch.get("secret_env"):
                entry["secret_env"] = str(ch["secret_env"]).strip()
            for k in CHANNEL_KEYS:
                v = ch.get(k)
                if v is not None and v != "":
                    entry[k] = coerce(channel_field(k), v)
            out.append(entry)

        def mutate(doc):
            doc["channels"] = out
        self.update(mutate)

    # ---------- prompts ----------
    def prompt_path(self, which: str) -> Path:
        """'system' or a channel name/slug -> file path."""
        if which == "system":
            return self.prompts_dir / "system.md"
        return self.prompts_dir / "channels" / f"{prompt_slug(which)}.md"


def validate(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("config must be a mapping")
    idx = field_index()
    for path, f in idx.items():
        cur, found = data, True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                found = False
                break
        if found and cur is not None:
            coerce(f, cur)
    if not str(data.get("bot_name", "x")).strip():
        raise ValueError("bot_name can't be empty")
    slots, names = set(), set()
    for ch in data.get("channels") or []:
        if not isinstance(ch, dict):
            raise ValueError("each channel must be a mapping")
        name, slot = str(ch.get("name") or "").strip(), ch.get("slot")
        if not name or slot is None:
            raise ValueError(f"channel needs name and slot: {ch}")
        if name.startswith("@"):
            raise ValueError(f"channel name {name} can't start with @ (reserved for direct messages)")
        if len(name.encode()) > 31:
            raise ValueError(f"channel name {name} too long (31 bytes max)")
        if not 0 <= int(slot) <= 39:
            raise ValueError(f"bad slot {slot} (0-39)")
        if int(slot) in slots:
            raise ValueError(f"slot {slot} used twice")
        if name.lower() in names:
            raise ValueError(f"channel {name} listed twice")
        slots.add(int(slot))
        names.add(name.lower())
        for k in CHANNEL_KEYS:
            if ch.get(k) is not None:
                coerce(channel_field(k), ch[k])


def dm_name(sender: str) -> str:
    """History/conversation name for DMs with one sender. Channels can't start with '@'."""
    return "@" + str(sender).strip()


def is_dm(name: str) -> bool:
    return str(name).startswith("@")


def prompt_slug(name: str) -> str:
    if is_dm(name):
        return "direct-messages"  # one notes file for every DM conversation
    return SLUG_RE.sub("", name.lower().lstrip("#")) or "channel"


DEFAULTS = Path(__file__).parent / "defaults"


def bootstrap(conf_dir: Path = CONF) -> list[str]:
    """First run: copy the starter config.yaml and prompts/system.md into conf/ if they're missing.
    Never overwrites. Returns what was created."""
    made = []
    for rel in ("config.yaml", "prompts/system.md"):
        dst = conf_dir / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULTS / rel, dst)
            made.append(rel)
    return made


def load_prompt(channel_name: str, prompts_dir: Path) -> str:
    """system.md plus prompts/channels/<slug>.md when present. Read fresh every call (that is the hot reload)."""
    parts = []
    base = prompts_dir / "system.md"
    if base.exists():
        parts.append(base.read_text().strip())
    extra = prompts_dir / "channels" / f"{prompt_slug(channel_name)}.md"
    if extra.exists() and extra.read_text().strip():
        parts.append("## Notes for this channel\n" + extra.read_text().strip())
    return "\n\n".join(parts)
