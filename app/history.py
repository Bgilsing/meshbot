"""SQLite store: every channel message heard or sent, and every LLM decision."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Msg:
    id: int
    channel: str
    ts: float          # local receive time (epoch)
    sender: str
    text: str
    own: bool
    snr: float | None = None
    hops: int | None = None


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, ts REAL NOT NULL, sender TEXT NOT NULL,
                text TEXT NOT NULL, own INTEGER NOT NULL DEFAULT 0, snr REAL, hops INTEGER, sender_ts INTEGER);
            CREATE INDEX IF NOT EXISTS ix_msg_chan_ts ON messages(channel, ts);
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, channel TEXT NOT NULL, trigger_id INTEGER,
                outcome TEXT, respond INTEGER, reason TEXT, rec TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_dec_trigger ON decisions(trigger_id);
            CREATE INDEX IF NOT EXISTS ix_dec_chan_ts ON decisions(channel, ts);
        """)
        self.db.commit()

    # ---------- messages ----------
    def add(self, channel: str, sender: str, text: str, own: bool = False, snr=None, hops=None,
            sender_ts: int | None = None, ts: float | None = None) -> Msg | None:
        """Store a message. Returns None for a duplicate (same channel/sender/text/sender timestamp)."""
        with self.lock:
            if sender_ts is not None:
                dup = self.db.execute(
                    "SELECT 1 FROM messages WHERE channel=? AND sender=? AND text=? AND sender_ts=?",
                    (channel, sender, text, sender_ts)).fetchone()
                if dup:
                    return None
            ts = time.time() if ts is None else ts
            cur = self.db.execute(
                "INSERT INTO messages(channel, ts, sender, text, own, snr, hops, sender_ts) VALUES (?,?,?,?,?,?,?,?)",
                (channel, ts, sender, text, int(own), snr, hops, sender_ts))
            self.db.commit()
        return Msg(cur.lastrowid, channel, ts, sender, text, own, snr, hops)

    @staticmethod
    def _msg(r) -> Msg:
        return Msg(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6], r[7])

    def recent(self, channel: str, limit: int = 30, max_age_h: float = 12) -> list[Msg]:
        since = time.time() - max_age_h * 3600
        with self.lock:
            rows = self.db.execute(
                "SELECT id, channel, ts, sender, text, own, snr, hops FROM messages WHERE channel=? AND ts>=? "
                "ORDER BY id DESC LIMIT ?", (channel, since, int(limit))).fetchall()
        return [self._msg(r) for r in reversed(rows)]

    def messages(self, channel: str | None = None, limit: int = 100, before_id: int | None = None,
                 after_id: int | None = None) -> list[dict]:
        """Newest first, each with its decision summary attached (for the UI)."""
        q = ("SELECT m.id, m.channel, m.ts, m.sender, m.text, m.own, m.snr, m.hops, "
             "d.id, d.outcome, d.reason, d.respond FROM messages m "
             "LEFT JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE trigger_id = m.id) WHERE 1=1")
        args: list = []
        if channel:
            q += " AND m.channel=?"
            args.append(channel)
        if before_id:
            q += " AND m.id<?"
            args.append(before_id)
        if after_id:
            q += " AND m.id>?"
            args.append(after_id)
        q += " ORDER BY m.id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = asdict(self._msg(r))
            d["decision"] = None if r[8] is None else {"id": r[8], "outcome": r[9], "reason": r[10],
                                                         "respond": bool(r[11])}
            out.append(d)
        return out

    def dm_conversations(self, days: float = 30) -> list[str]:
        """DM conversation names ("@Name") with traffic in the last `days`, most recent first."""
        with self.lock:
            rows = self.db.execute("SELECT channel, MAX(ts) t FROM messages WHERE channel LIKE '@%' AND ts>=? "
                                   "GROUP BY channel ORDER BY t DESC LIMIT 50",
                                   (time.time() - days * 86400,)).fetchall()
        return [r[0] for r in rows]

    def counts(self, since: float) -> dict:
        with self.lock:
            heard = self.db.execute("SELECT COUNT(*) FROM messages WHERE ts>=? AND own=0", (since,)).fetchone()[0]
            rows = self.db.execute("SELECT outcome, COUNT(*) FROM decisions WHERE ts>=? GROUP BY outcome",
                                   (since,)).fetchall()
        return {"heard": heard, "decisions": {o or "?": n for o, n in rows}}

    # ---------- decisions ----------
    def add_decision(self, rec: dict, trigger_id: int | None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO decisions(ts, channel, trigger_id, outcome, respond, reason, rec) VALUES (?,?,?,?,?,?,?)",
                (time.time(), rec.get("channel"), trigger_id, rec.get("outcome"), int(bool(rec.get("respond"))),
                 rec.get("reason"), json.dumps(rec, ensure_ascii=False)))
            self.db.commit()
        return cur.lastrowid

    def decisions(self, channel: str | None = None, outcome: str | None = None, limit: int = 50,
                  before_id: int | None = None, q: str | None = None) -> list[dict]:
        sql = "SELECT id, rec FROM decisions WHERE 1=1"
        args: list = []
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        if outcome:
            sql += " AND outcome LIKE ?"
            args.append(outcome + "%")
        if before_id:
            sql += " AND id<?"
            args.append(before_id)
        if q:
            sql += " AND rec LIKE ?"
            args.append(f"%{q}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [{"id": r[0], **json.loads(r[1])} for r in rows]

    def decision(self, did: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT id, rec FROM decisions WHERE id=?", (did,)).fetchone()
        return {"id": r[0], **json.loads(r[1])} if r else None

    def prune(self, keep_days: float = 30) -> None:
        cutoff = time.time() - keep_days * 86400
        with self.lock:
            self.db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM decisions WHERE ts < ?", (cutoff,))
            self.db.commit()
