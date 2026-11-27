"""meshbot: an LLM that listens on MeshCore channels (through openHop) and answers when it judges it should."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime

import httpx

from .brain import Brain, Decision
from .compress import blen, clean, fit, part_budget
from .config import DATA, Config, bootstrap, load_prompt
from .history import History, Msg
from .mesh import Incoming, Mesh
from .meshdata import MeshTools

log = logging.getLogger("meshbot")

DECISIONS = DATA / "decisions.jsonl"


def msg_dict(m: Msg) -> dict:
    return {"ts": m.ts, "sender": m.sender, "text": m.text, "own": m.own}


class LogBuffer(logging.Handler):
    """Last N log lines, for the web UI."""

    def __init__(self, n: int = 500):
        super().__init__()
        self.lines: deque = deque(maxlen=n)
        self.seq = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.seq += 1
        self.lines.append((self.seq, record.levelname, self.format(record)))


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.history = History(DATA / "history.db")
        self.brain = Brain(cfg)
        self.mesh = Mesh(cfg, self.on_message)
        self.meshtools = MeshTools(cfg, self.mesh)
        self.brain.meshtools = self.meshtools
        self.gen: dict[str, int] = defaultdict(int)       # bumps on every new message per channel
        self.timers: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.sent: list[tuple[float, str, str]] = []       # (time, channel, trigger sender) per reply
        self.last_reply: dict[str, float] = {}
        self.started = time.time()
        self.llm_status: dict = {}                         # last call: ok/error, latency, time
        self.logbuf: LogBuffer | None = None

    # ---------- intake ----------
    async def on_message(self, inc: Incoming) -> None:
        self.cfg.reload()
        ch = self.cfg.channel_by_slot(inc.slot)
        if not ch:
            log.debug("message on unconfigured slot %s ignored", inc.slot)
            return
        name = ch["name"]
        if inc.sender.lower() == self.cfg.bot_name.lower():
            return  # our own packet echoed back; already stored when sent
        m = self.history.add(name, inc.sender, inc.text, snr=inc.snr, hops=inc.hops, sender_ts=inc.sender_ts)
        if m is None:
            return
        log.info("[%s] %s: %s%s", name, inc.sender, inc.text, "" if inc.live else "  (backlog)")
        if not inc.live or ch.get("mode") == "off" or self.cfg.ignored(inc.sender):
            return
        self.gen[name] += 1
        old = self.timers.get(name)
        if old and not old.done():
            old.cancel()
        self.timers[name] = asyncio.create_task(self._debounce(name, self.gen[name], float(ch["debounce_s"])))

    async def _debounce(self, name: str, gen: int, delay: float) -> None:
        await asyncio.sleep(delay)
        # Evaluate in its own task so a later message's cancel() only ever hits a sleeping timer.
        asyncio.create_task(self.evaluate(name, gen))

    # ---------- thinking ----------
    def _track_llm(self, d: Decision) -> None:
        self.llm_status = {"time": time.time(), "ok": d.error is None, "error": d.error, "latency_s": d.latency_s}

    async def evaluate(self, name: str, gen: int) -> None:
        async with self.locks[name]:
            if gen != self.gen[name]:
                return
            self.cfg.reload()
            ch = self.cfg.channel_by_name(name)
            if not ch or ch.get("mode") == "off":
                return
            msgs = self.history.recent(name, int(ch["history"]), float(ch["history_max_age_h"]))
            if not msgs or msgs[-1].own or self.cfg.ignored(msgs[-1].sender):
                return
            trigger = msgs[-1]
            d = await self.brain.decide(load_prompt(name, self.cfg.prompts_dir), ch, msgs)
            self._track_llm(d)
            rec = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "channel": name,
                   "mode": ch.get("mode"), "eagerness": ch.get("eagerness"),
                   "model": d.model, "fallback": d.fallback, "attempts": d.attempts,
                   "trigger": {"id": trigger.id, "sender": trigger.sender, "text": trigger.text},
                   "thread": [msg_dict(m) for m in msgs],
                   "respond": d.respond, "reason": d.reason, "parts_raw": d.parts, "raw": d.raw[:4000],
                   "latency_s": d.latency_s, "usage": d.usage, "error": d.error, "searches": d.searches,
                   "web_search": bool(ch.get("web_search"))}
            try:
                rec.update(await self._act(name, ch, gen, trigger, d))
            finally:
                self._log_decision(rec, trigger.id)

    async def fit_reply(self, ch: dict, parts: list[str]) -> dict:
        """Fit model output to packets, with one shorten re-ask if it overflows."""
        bot, ptb, maxp = self.cfg.bot_name, int(ch["packet_text_bytes"]), int(ch["max_parts"])
        out: dict = {}
        f = fit(parts, bot, ptb, maxp, allow_truncate=False)
        if f.overflow:
            shorter = await self.brain.shorten(parts, ch)
            out["shortened"] = shorter
            f = fit(shorter or parts, bot, ptb, maxp, allow_truncate=True)
        out.update({"parts": f.parts, "resplit": f.resplit, "truncated": f.truncated})
        return out

    async def _act(self, name: str, ch: dict, gen: int, trigger: Msg, d: Decision) -> dict:
        if d.error:
            log.warning("[%s] no decision (%s)", name, d.error)
            return {"outcome": "error"}
        if not d.respond:
            log.info("[%s] stay quiet: %s (%.1fs)", name, d.reason, d.latency_s)
            return {"outcome": "silent"}
        if gen != self.gen[name]:
            log.info("[%s] newer messages arrived while thinking; re-evaluating", name)
            return {"outcome": "superseded"}

        out = await self.fit_reply(ch, d.parts)
        if ch.get("mode") != "live" or not self.cfg.enabled:
            why = "SHADOW" if ch.get("mode") != "live" else "DISABLED"
            log.info("[%s] %s would send (%s): %s", name, why, d.reason, " | ".join(out["parts"]))
            out["outcome"] = "shadow" if ch.get("mode") != "live" else "disabled"
            return out
        blocked = self._guard(name, ch, trigger.sender)
        if blocked:
            log.info("[%s] reply blocked by %s: %s", name, blocked, " | ".join(out["parts"]))
            out["outcome"] = "blocked:" + blocked
            return out

        self.sent.append((time.time(), name, trigger.sender))
        self.last_reply[name] = time.time()
        sent = await self._transmit(name, ch, out["parts"])
        out["outcome"] = "sent" if sent == len(out["parts"]) else f"send_failed_after_{sent}"
        return out

    async def _transmit(self, name: str, ch: dict, parts: list[str]) -> int:
        sent = 0
        for i, part in enumerate(parts):
            if i:
                await asyncio.sleep(float(ch["part_gap_s"]))
            if not await self.mesh.send(int(ch["slot"]), part):
                break
            self.history.add(name, self.cfg.bot_name, part, own=True)
            sent += 1
            log.info("[%s] SENT %s", name, part)
        return sent

    def _guard(self, name: str, ch: dict, sender: str) -> str | None:
        now = time.time()
        self.sent = [s for s in self.sent if now - s[0] < 3600]
        if now - self.last_reply.get(name, 0) < float(ch["cooldown_s"]):
            return "cooldown"
        if sum(1 for s in self.sent if s[1] == name) >= int(ch["max_replies_per_hour"]):
            return "channel_hourly_cap"
        if sum(1 for s in self.sent if s[2].lower() == sender.lower()) >= int(ch["per_sender_per_hour"]):
            return "sender_hourly_cap"
        return None

    def _log_decision(self, rec: dict, trigger_id: int | None) -> None:
        rec["id"] = self.history.add_decision(rec, trigger_id)
        DATA.mkdir(parents=True, exist_ok=True)
        with DECISIONS.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---------- used by the web UI (never transmit unless named so) ----------
    async def playground(self, channel: str, thread: list[dict], prompt_override: str | None = None,
                         overrides: dict | None = None) -> dict:
        ch = self.cfg.channel_by_name(channel) or {**self.cfg.channel_defaults(), "name": channel}
        ch = {**ch, **(overrides or {})}
        now = time.time()
        msgs = [Msg(i, channel, t.get("ts") or now - (len(thread) - i) * 30, t["sender"], t["text"],
                    bool(t.get("own")), hops=t.get("hops")) for i, t in enumerate(thread)]
        prompt = prompt_override if prompt_override is not None else load_prompt(channel, self.cfg.prompts_dir)
        d = await self.brain.decide(prompt, ch, msgs)
        self._track_llm(d)
        res = {"respond": d.respond, "reason": d.reason, "parts_raw": d.parts, "raw": d.raw,
               "latency_s": d.latency_s, "usage": d.usage, "error": d.error, "system": d.system, "user": d.user,
               "searches": d.searches, "model": d.model, "fallback": d.fallback, "attempts": d.attempts}
        if d.respond and not d.error:
            res.update(await self.fit_reply(ch, d.parts))
            res["bytes"] = [blen(f"{self.cfg.bot_name}: {p}") for p in res["parts"]]
        return res

    async def replay(self, decision_id: int, prompt_override: str | None = None) -> dict:
        rec = self.history.decision(decision_id)
        if not rec:
            raise KeyError("no such decision")
        new = await self.playground(rec["channel"], rec["thread"], prompt_override)
        return {"old": {k: rec.get(k) for k in ("id", "time", "channel", "trigger", "respond", "reason", "parts",
                                                "outcome")}, "new": new}

    async def manual_send(self, channel: str, text: str) -> dict:
        ch = self.cfg.channel_by_name(channel)
        if not ch:
            raise KeyError("unknown channel")
        text = clean(text)
        budget = part_budget(int(ch["packet_text_bytes"]), self.cfg.bot_name, 1)
        if not text or blen(text) > budget:
            raise ValueError(f"message must be 1-{budget} bytes (is {blen(text)})")
        n = await self._transmit(channel, ch, [text])
        if not n:
            raise RuntimeError("send failed (companion not connected?)")
        return {"sent": text}

    def model_health(self) -> list[dict]:
        """Every model any channel uses (primary/fallback), with failover state."""
        seen: dict[str, dict] = {}
        chains = [self.brain.model_chain(None)] + [self.brain.model_chain(c) for c in self.cfg.channels()]
        for chain in chains:
            for i, m in enumerate(chain):
                role = "primary" if i == 0 else "fallback"
                e = seen.setdefault(m, {"model": m, "roles": set()})
                e["roles"].add(role)
        now = time.time()
        out = []
        for m, e in seen.items():
            h = self.brain.health.get(m, {})
            out.append({"model": m, "roles": sorted(e["roles"], reverse=True), "ok": h.get("ok"),
                        "last_ok": h.get("last_ok"), "last_error": h.get("last_error"),
                        "error_time": h.get("error_time"),
                        "held_for_s": max(0, round(h.get("down_until", 0) - now))})
        return out

    def status(self) -> dict:
        now = time.time()
        self.sent = [s for s in self.sent if now - s[0] < 3600]
        chans = []
        for ch in self.cfg.channels():
            n = ch["name"]
            chain = self.brain.model_chain(ch)
            chans.append({"name": n, "slot": ch["slot"], "mode": ch["mode"], "eagerness": ch["eagerness"],
                          "model": chain[0], "fallback_model": chain[1] if len(chain) > 1 else None,
                          "private": not n.startswith("#"),
                          "link": self.mesh.channel_status.get(n),
                          "replies_last_hour": sum(1 for s in self.sent if s[1] == n),
                          "last_reply": self.last_reply.get(n)})
        return {
            "bot_name": self.cfg.bot_name, "enabled": self.cfg.enabled, "uptime_s": round(now - self.started),
            "companion": {"connected": self.mesh.connected, "name": self.mesh.companion_name,
                          "address": "{host}:{port}".format(**self.cfg.section("companion")),
                          "error": self.mesh.last_error, "sent_packets": self.mesh.sent_count,
                          "path_hash_bytes": getattr(self.mesh, "path_hash_bytes", None)},
            "models": self.model_health(),
            "llm": {**self.llm_status, "model": self.cfg.value("llm.model"),
                    "key_set": bool(self.cfg.llm_api_key)},
            "config_error": self.cfg.last_error,
            "mesh_data": {"openhop_key_set": bool(self.cfg.openhop_api_key), "error": self.meshtools.last_error,
                          "radio_last_hour": self.meshtools.radio_used_last_hour(),
                          "radio_max_per_hour": int(self.cfg.value("mesh.radio_max_per_hour")),
                          "radio_recent": [{"ts": r[0], "kind": r[1], "node": r[2], "outcome": r[3]}
                                           for r in list(self.meshtools.radio_log)[-5:]]},
            "channels": chans,
            "last_24h": self.history.counts(now - 86400),
        }

    # ---------- housekeeping ----------
    async def heartbeat(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                url = self.cfg.value("heartbeat_url")
                if url and self.mesh.connected:
                    try:
                        await client.get(url, params={"status": "up", "msg": "OK"}, timeout=10)
                    except Exception as e:
                        log.debug("heartbeat failed: %s", e)
                await asyncio.sleep(60)

    async def prune(self) -> None:
        while True:
            self.history.prune(keep_days=30)
            await asyncio.sleep(86400)

    async def run(self) -> None:
        from .web import start_web
        log.info("meshbot starting as '%s'", self.cfg.bot_name)
        await start_web(self)
        await asyncio.gather(self.mesh.run(), self.heartbeat(), self.prune())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("meshcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    buf = LogBuffer()
    logging.getLogger().addHandler(buf)
    for rel in bootstrap():
        log.warning("created starter conf/%s; edit it or use the web UI", rel)
    bot = Bot(Config())
    bot.logbuf = buf
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
