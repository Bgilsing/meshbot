"""Amateur radio callsign lookup: first name, last initial and city/state only.

Sources, in order:
  - QRZ.com XML data service (worldwide), when a QRZ username/password is set in Settings > Secrets.
  - callook.info (free, US FCC data, no account).
Everything else the sources return (street address, email, coordinates, license dates, FRN...) is dropped here,
in code, so it never reaches the model.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

from . import tools as T

log = logging.getLogger("meshbot.callsign")

QRZ_URL = "https://xmldata.qrz.com/xml/current/"
CALLOOK_URL = "https://callook.info/{call}/json"
NS = {"q": "http://xmldata.qrz.com"}
CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,4}/)?([A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4})(?:/[A-Z0-9]{1,4})?$")
CACHE_S = 86400


def normalize(call: str) -> str:
    """'n0call/p' -> 'N0CALL' (base call without prefixes/suffixes). Raises ToolError if it isn't a callsign."""
    c = re.sub(r"\s+", "", str(call or "")).upper()
    m = CALL_RE.match(c)
    if not m:
        raise T.ToolError(f"{call!r} doesn't look like an amateur callsign")
    return m.group(1)


def _title(s: str) -> str:
    return " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in str(s or "").split())


def person(first: str, last: str) -> str:
    first = _title((first or "").split()[0]) if (first or "").strip() else ""
    initial = (last or "").strip()[:1].upper()
    return " ".join(x for x in (first, f"{initial}." if initial else "") if x) or "name not listed"


def place(city: str, state: str, country: str = "") -> str:
    bits = [_title(city), (state or "").strip().upper() if len((state or "").strip()) <= 3 else _title(state)]
    if country and country.strip().lower() not in ("united states", "usa", "us"):
        bits.append(_title(country))
    return ", ".join(b for b in bits if b) or "location not listed"


class CallsignLookup:
    def __init__(self, cfg, client):
        self.cfg = cfg
        self._client = client  # an httpx.AsyncClient, or a function returning one
        self._key: str | None = None
        self._cache: dict[str, tuple[float, str, dict]] = {}

    @property
    def client(self):
        return self._client() if callable(self._client) else self._client

    # ---------- QRZ ----------
    def _qrz_creds(self) -> tuple[str, str] | None:
        u, p = self.cfg.secrets.get("qrz_username"), self.cfg.secrets.get("qrz_password")
        return (u, p) if u and p else None

    async def _qrz_get(self, params: dict) -> ET.Element:
        r = await self.client.get(QRZ_URL, params={**params, "agent": "meshbot"}, timeout=15)
        r.raise_for_status()
        try:
            return ET.fromstring(r.content)
        except ET.ParseError:
            raise T.ToolError("QRZ returned something unreadable")

    async def _qrz_login(self) -> str:
        u, p = self._qrz_creds()
        root = await self._qrz_get({"username": u, "password": p})
        key = root.findtext("q:Session/q:Key", namespaces=NS)
        if not key:
            err = (root.findtext("q:Session/q:Error", namespaces=NS) or "login failed").strip()
            raise T.ToolError(f"QRZ login: {err}")
        self._key = key
        return key

    async def qrz(self, call: str) -> dict | None:
        """{'first', 'last', 'city', 'state', 'country'} or None if QRZ has no record."""
        for attempt in range(2):
            key = self._key or await self._qrz_login()
            root = await self._qrz_get({"s": key, "callsign": call})
            err = (root.findtext("q:Session/q:Error", namespaces=NS) or "").strip()
            rec = root.find("q:Callsign", NS)
            if rec is not None:
                g = lambda tag: (rec.findtext(f"q:{tag}", namespaces=NS) or "").strip()
                return {"first": g("fname"), "last": g("name"), "city": g("addr2"), "state": g("state"),
                        "country": g("country"), "source": "QRZ"}
            if err.lower().startswith("not found"):
                return None
            if attempt == 0 and re.search(r"session|key", err, re.I):
                self._key = None  # expired session: log in again once
                continue
            raise T.ToolError(f"QRZ: {err or 'no data'}")
        return None

    # ---------- callook (US) ----------
    async def callook(self, call: str) -> dict | None:
        r = await self.client.get(CALLOOK_URL.format(call=call), timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "VALID":
            return None
        line2 = str((d.get("address") or {}).get("line2") or "")
        m = re.match(r"^(.*?),\s*([A-Z]{2})\b", line2.strip())
        city, state = (m.group(1), m.group(2)) if m else (line2, "")
        name = str(d.get("name") or "")
        if d.get("type") == "CLUB":
            return {"club": name.strip(), "city": city, "state": state, "country": "", "source": "FCC (callook.info)"}
        if "," in name:  # "LAST, FIRST M"
            last, first = [x.strip() for x in name.split(",", 1)]
        else:            # "FIRST M LAST"
            parts = name.split()
            first, last = (parts[0], parts[-1]) if parts else ("", "")
        return {"first": first, "last": last, "city": city, "state": state, "country": "",
                "source": "FCC (callook.info)"}

    # ---------- public ----------
    async def lookup(self, raw: str) -> tuple[str, dict]:
        call = normalize(raw)
        hit = self._cache.get(call)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1], hit[2]
        rec, notes = None, []
        if self._qrz_creds():
            try:
                rec = await self.qrz(call)
            except Exception as e:
                notes.append(f"QRZ unavailable ({str(e)[:80]})")
                log.warning("QRZ lookup of %s failed: %s", call, e)
        if rec is None:
            try:
                rec = await self.callook(call)
            except Exception as e:
                notes.append(f"FCC lookup unavailable ({type(e).__name__})")
        if rec is None:
            text = f"{call}: no license record found" + (f" ({'; '.join(notes)})" if notes else "") + "."
            return text, {"found": False}
        if rec.get("club"):
            who = f"{rec['club']} (club station)"
        else:
            who = person(rec.get("first", ""), rec.get("last", ""))
        text = f"{call}: {who}, {place(rec.get('city', ''), rec.get('state', ''), rec.get('country', ''))}."
        meta = {"found": True, "source": rec["source"]}
        self._cache[call] = (time.time(), text, meta)
        return text, meta
