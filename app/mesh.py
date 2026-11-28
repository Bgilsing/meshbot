"""Connection to the openHop companion (MeshCore companion protocol over TCP) via the meshcore library."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from meshcore import EventType, MeshCore

from .config import Config

log = logging.getLogger("meshbot.mesh")

BACKLOG_GRACE_S = 15  # messages drained right after (re)connect are history, not live triggers


@dataclass
class Incoming:
    slot: int
    sender: str
    text: str
    sender_ts: int | None
    snr: float | None
    hops: int | None
    live: bool  # False while draining the companion's queued backlog after connect
    dm: bool = False               # a direct message to the bot (slot is then -1)
    sender_key: str | None = None  # DMs: sender's public key prefix (hex), where replies go


class Mesh:
    def __init__(self, cfg: Config, on_message: Callable[[Incoming], Awaitable[None]]):
        self.cfg = cfg
        self.on_message = on_message
        self.mc: MeshCore | None = None
        self.connected_at = 0.0
        self._lost = asyncio.Event()
        self._send_lock = asyncio.Lock()
        # status for the web UI
        self.companion_name: str | None = None
        self.channel_status: dict[str, str] = {}
        self.last_error: str | None = None
        self.sent_count = 0
        self.path_hash_bytes: int | None = None  # as confirmed on the companion

    @property
    def connected(self) -> bool:
        return bool(self.mc and self.mc.is_connected)

    async def run(self) -> None:
        backoff = 2
        while True:
            comp = self.cfg.section("companion")
            host, port = comp["host"], int(comp["port"])
            try:
                self.mc = await MeshCore.create_tcp(host, port, default_timeout=10)
                if self.mc is None:
                    raise ConnectionError("companion did not answer APP_START")
                sig = self.cfg.link_signature()
                await self._setup()
                backoff = 2
                await self._wait_until_lost(sig)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{host}:{port}: {e}"
                log.warning("companion %s:%s unavailable: %s", host, port, e)
            await self._close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _setup(self) -> None:
        mc = self.mc
        self._lost.clear()
        me = mc.self_info or {}
        name = me.get("name", "?")
        self.companion_name, self.last_error = name, None
        self.channel_status = {}
        log.info("connected to companion '%s'", name)
        if name != self.cfg.bot_name:
            log.warning("companion name '%s' != bot_name '%s' in config.yaml; prompts and byte budgets use "
                        "bot_name, so make them match", name, self.cfg.bot_name)
        await self._apply_path_hash()
        for ch in self.cfg.channels():
            try:
                secret = self.cfg.channel_secret(ch)
            except ValueError as e:
                self.channel_status[ch["name"]] = str(e)
                log.error("%s", e)
                continue
            r = await mc.commands.set_channel(int(ch["slot"]), ch["name"], secret)
            if r.type == EventType.ERROR:
                self.channel_status[ch["name"]] = f"set_channel failed: {r.payload}"
                log.error("set_channel %s slot %s failed: %s", ch["name"], ch["slot"], r.payload)
            else:
                self.channel_status[ch["name"]] = "ok"
                log.info("channel %s -> slot %s (%s)", ch["name"], ch["slot"], "private" if secret else "hashtag")
        try:
            await mc.ensure_contacts()  # names for incoming DMs (they only carry a key prefix)
        except Exception as e:
            log.warning("could not load contacts: %s", e)
        mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_direct_msg)
        mc.subscribe(EventType.DISCONNECTED, lambda _e: self._lost.set())
        self.connected_at = time.time()
        await mc.start_auto_message_fetching()

    async def _apply_path_hash(self) -> None:
        """Set the companion's path hash size (mode 0/1/2 = 1/2/3 bytes) to the configured value."""
        want = int(self.cfg.value("companion.path_hash_bytes")) - 1
        try:
            have = await self.mc.commands.get_path_hash_mode()
            if have != want:
                r = await self.mc.commands.set_path_hash_mode(want)
                if r is None or r.type == EventType.ERROR:
                    raise RuntimeError(f"companion refused: {None if r is None else r.payload}")
                log.info("path hash size set to %d byte(s) (was %d)", want + 1, have + 1)
            self.path_hash_bytes = want + 1
        except Exception as e:
            self.path_hash_bytes = None
            log.error("could not set path hash size to %d byte(s): %s", want + 1, e)

    async def _wait_until_lost(self, sig) -> None:
        while not self._lost.is_set():
            try:
                await asyncio.wait_for(self._lost.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            if not self.connected:
                log.warning("companion link dropped")
                return
            self.cfg.reload()
            if self.cfg.link_signature() != sig:
                log.info("companion/channel config changed, reconnecting")
                return

    async def _close(self) -> None:
        if self.mc:
            try:
                await self.mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await self.mc.disconnect()
            except Exception:
                pass
        self.mc = None
        self.companion_name = None

    async def _on_channel_msg(self, event) -> None:
        p = event.payload or {}
        if p.get("txt_type", 0) != 0:
            return
        raw = str(p.get("text", ""))
        sender, sep, body = raw.partition(": ")
        if not sep:
            sender, body = "?", raw
        hops = p.get("path_len")
        msg = Incoming(slot=int(p.get("channel_idx", -1)), sender=sender.strip(), text=body.strip(),
                       sender_ts=p.get("sender_timestamp"), snr=p.get("SNR"),
                       hops=hops if isinstance(hops, int) and hops < 255 else None,
                       live=time.time() - self.connected_at > BACKLOG_GRACE_S)
        try:
            await self.on_message(msg)
        except Exception:
            log.exception("message handler failed")

    async def contact_name(self, prefix: str) -> str:
        """Name for a DM sender's key prefix: contact cache, then one refresh, else 'node <prefix>'."""
        mc = self.mc
        for attempt in range(2):
            c = mc.get_contact_by_key_prefix(prefix) if mc else None
            if c and c.get("adv_name"):
                return str(c["adv_name"]).strip()
            if attempt == 0 and mc:
                try:
                    await mc.commands.get_contacts()
                except Exception as e:
                    log.debug("contact refresh failed: %s", e)
        return f"node {prefix[:8]}"

    async def _on_direct_msg(self, event) -> None:
        p = event.payload or {}
        if p.get("txt_type", 0) != 0:  # CLI commands / signed data, not chat
            return
        prefix = str(p.get("pubkey_prefix") or "")
        if not prefix:
            return
        hops = p.get("path_len")
        msg = Incoming(slot=-1, sender=await self.contact_name(prefix), text=str(p.get("text", "")).strip(),
                       sender_ts=p.get("sender_timestamp"), snr=p.get("SNR"),
                       hops=hops if isinstance(hops, int) and hops < 255 else None,
                       live=time.time() - self.connected_at > BACKLOG_GRACE_S, dm=True, sender_key=prefix)
        try:
            await self.on_message(msg)
        except Exception:
            log.exception("DM handler failed")

    async def send_dm(self, key_prefix: str, text: str) -> bool:
        """Send a direct message, waiting for the ACK (retries, then flood). True when acknowledged."""
        async with self._send_lock:
            if not self.connected:
                log.warning("not connected, dropping DM")
                return False
            try:
                r = await self.mc.commands.send_msg_with_retry(key_prefix, text)
            except Exception as e:
                log.warning("DM to %s failed: %s", key_prefix[:8], e)
                return False
            if r is None or r.type == EventType.ERROR:
                log.warning("DM to %s not acknowledged: %s", key_prefix[:8], None if r is None else r.payload)
                return False
            self.sent_count += 1
            return True

    async def send_advert(self, flood: bool) -> None:
        async with self._send_lock:
            if not self.connected:
                raise RuntimeError("companion not connected")
            r = await self.mc.commands.send_advert(flood=flood)
            if r is None or r.type == EventType.ERROR:
                raise RuntimeError(f"companion refused advert: {None if r is None else r.payload}")
            self.sent_count += 1

    async def send(self, slot: int, text: str) -> bool:
        async with self._send_lock:
            if not self.connected:
                log.warning("not connected, dropping send")
                return False
            try:
                r = await self.mc.commands.send_chan_msg(int(slot), text)
            except Exception as e:
                log.warning("send failed: %s", e)
                return False
            if r is None or r.type == EventType.ERROR:
                log.warning("send refused: %s", None if r is None else r.payload)
                return False
            self.sent_count += 1
            return True

    async def rename(self, name: str) -> str:
        """Set the companion's advertised node name (what other nodes see as the sender)."""
        if not self.connected:
            raise RuntimeError("companion not connected")
        r = await self.mc.commands.set_name(name)
        if r is None or r.type == EventType.ERROR:
            raise RuntimeError(f"companion refused rename: {None if r is None else r.payload}")
        self.companion_name = name
        return name

    # ---------- radio queries (transmit; callers rate-limit) ----------
    def _need(self):
        if not self.connected:
            raise RuntimeError("companion not connected")
        return self.mc

    async def get_contact(self, pubkey: str) -> dict | None:
        """The companion's contact record for a node, or None if it isn't a contact."""
        mc = self._need()
        r = await mc.commands.get_contact_by_key(bytes.fromhex(pubkey))
        return r.payload if r is not None and r.type != EventType.ERROR else None

    async def advert_path(self, pubkey: str) -> dict | None:
        """Path the node's last advert took to reach us ({path_len, path, path_hash_mode}), if one was heard.
        Local lookup on the companion, nothing is transmitted."""
        mc = self._need()
        r = await mc.commands.get_advert_path(pubkey)
        if r is None or r.type == EventType.ERROR or r.payload.get("path_len", -1) < 0:
            return None
        return r.payload

    async def ensure_contact(self, node: dict) -> dict:
        """Make sure the companion knows the node (needed for login/requests/path discovery). Adds it with a
        flood route from CoreScope's record (full public key, name, role, position)."""
        c = await self.get_contact(node["public_key"])
        if c:
            return c
        mc = self._need()
        ctype = {"repeater": 2, "room": 3, "sensor": 4}.get(str(node.get("role")), 1)
        c = {"public_key": node["public_key"], "type": ctype, "flags": 0, "out_path_len": -1,
             "out_path_hash_mode": 0, "out_path": "", "adv_name": str(node.get("name") or "")[:31],
             "last_advert": int(time.time()), "adv_lat": float(node.get("lat") or 0),
             "adv_lon": float(node.get("lon") or 0)}
        r = await mc.commands.add_contact(c)
        if r is None or r.type == EventType.ERROR:
            raise RuntimeError(f"companion refused to add contact: {None if r is None else r.payload}")
        log.info("added contact %s (%s)", c["adv_name"], node["public_key"][:8])
        return c

    async def login(self, pubkey: str, password: str, timeout: float, attempts: int = 3) -> dict:
        """Log in to a repeater/room. Returns the LOGIN_SUCCESS payload; raises on refusal or when no attempt was
        answered within `timeout` seconds in total.

        Like a phone does, it retries: a busy repeater misses logins (collisions with other traffic are common).
        Every attempt first forgets the stored route and floods, because routes change (and a route learned under
        another path-hash size is useless). The reply teaches the companion the current route for what follows."""
        mc = self._need()
        prefix = pubkey[:12]
        ok = asyncio.ensure_future(mc.dispatcher.wait_for_event(EventType.LOGIN_SUCCESS,
                                                                attribute_filters={"pubkey_prefix": prefix},
                                                                timeout=timeout))
        bad = asyncio.ensure_future(mc.dispatcher.wait_for_event(EventType.LOGIN_FAILED,
                                                                 attribute_filters={"pubkey_prefix": prefix},
                                                                 timeout=timeout))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        attempts = max(1, int(attempts))
        try:
            for n in range(attempts):
                left = deadline - loop.time()
                if left <= 1:
                    break
                try:
                    rp = await mc.commands.reset_path(pubkey)
                    if rp is None or rp.type == EventType.ERROR:
                        log.debug("reset_path %s: %s", prefix[:8], None if rp is None else rp.payload)
                except Exception as e:
                    log.debug("reset_path %s failed: %s", prefix[:8], e)
                r = await mc.commands.send_login(pubkey, password)
                if r is None or r.type == EventType.ERROR:
                    raise RuntimeError(f"login not sent: {None if r is None else r.payload}")
                suggested = float((r.payload or {}).get("suggested_timeout") or 0) / 800  # ms, +25% like meshcore
                # Split what's left over the remaining attempts, but give each at least the companion's estimate.
                wait = min(left, max(suggested, left / (attempts - n)))
                log.info("login %s attempt %d/%d (flood), waiting %.0fs", prefix[:8], n + 1, attempts, wait)
                done, _ = await asyncio.wait({ok, bad}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                if ok in done and ok.result():
                    return ok.result().payload
                if bad in done and bad.result():
                    raise PermissionError("login refused (wrong guest password?)")
                if done:  # a waiter ended without an event (its own timeout)
                    break
                await asyncio.sleep(random.uniform(0.5, 2.5))  # don't collide with someone else's retry again
        finally:
            for t in (ok, bad):
                t.cancel()
        raise TimeoutError(f"no answer to {attempts} login attempts (repeater busy, out of range, or it needs a "
                           "guest password)")

    async def logout(self, pubkey: str) -> None:
        try:
            await self._need().commands.send_logout(pubkey)
        except Exception as e:
            log.debug("logout failed: %s", e)

    async def _with_retries(self, what: str, pubkey: str, call, timeout: float, attempts: int):
        """Run one over-the-air request with retries: the mesh drops packets. `call(t)` sends once and waits up
        to t seconds, returning None when nothing came back. `timeout` is the budget for all attempts. The last
        retry forgets the stored route first, so it floods in case the direct path is what's failing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        attempts = max(1, int(attempts))
        last_err = None
        for n in range(attempts):
            left = deadline - loop.time()
            if left <= 1:
                break
            flood = n > 0 and n == attempts - 1
            if flood:
                try:
                    await self._need().commands.reset_path(pubkey)
                except Exception as e:
                    log.debug("reset_path %s failed: %s", pubkey[:8], e)
            per = min(left, left / (attempts - n))
            log.info("%s %s attempt %d/%d%s, waiting %.0fs", what, pubkey[:8], n + 1, attempts,
                     " (flood)" if flood else "", per)
            try:
                r = await call(per)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                r, last_err = None, e
                log.info("%s %s attempt %d failed: %s", what, pubkey[:8], n + 1, e)
            if r:
                return r
            await asyncio.sleep(random.uniform(0.5, 2.5))
        if last_err and not isinstance(last_err, (TimeoutError, asyncio.TimeoutError)):
            raise last_err
        return None

    async def neighbours(self, pubkey: str, timeout: float, attempts: int = 3) -> dict | None:
        mc = self._need()
        return await self._with_retries(
            "neighbours", pubkey, lambda t: mc.commands.fetch_all_neighbours(
                pubkey, pubkey_prefix_length=4, timeout=t, min_timeout=min(5, t)), timeout, attempts)

    async def status_req(self, pubkey: str, timeout: float, attempts: int = 3) -> dict | None:
        mc = self._need()
        return await self._with_retries(
            "status", pubkey, lambda t: mc.commands.req_status_sync(pubkey, timeout=t, min_timeout=min(5, t)),
            timeout, attempts)

    async def path_discovery(self, pubkey: str, timeout: float, attempts: int = 3) -> dict | None:
        mc = self._need()

        async def once(t):
            r = await mc.commands.send_path_discovery_sync(pubkey, timeout=t, min_timeout=min(5, t))
            return r.payload if r is not None and r.type != EventType.ERROR else None
        return await self._with_retries("path discovery", pubkey, once, timeout, attempts)
