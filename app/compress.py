"""Make LLM text fit MeshCore channel packets.

A channel packet carries "<sender name>: <text>". openHop/firmware cap the text at ~160 bytes and some clients
show less, so the total is configurable (packet_text_bytes). The bot's name and ": " come out of that, and so does
the " 1/3" marker when a reply has several parts. All limits are in UTF-8 bytes, not characters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ELLIPSIS = "..."

_TYPO = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-",
                       "…": "...", " ": " ", "•": "-"})


def blen(s: str) -> int:
    return len(s.encode("utf-8"))


def cut_bytes(s: str, n: int) -> str:
    """At most n bytes of s, never splitting a UTF-8 character."""
    return s.encode("utf-8")[:max(n, 0)].decode("utf-8", "ignore")


def clean(text: str, bot_name: str = "") -> str:
    """Flatten model output to one line of plain text."""
    t = re.sub(r"<think>.*?</think>", " ", text or "", flags=re.S | re.I)
    t = t.translate(_TYPO)
    t = re.sub(r"```.*?```", lambda m: m.group(0).strip("`"), t, flags=re.S)
    t = t.replace("`", "")
    t = re.sub(r"\*\*|__|~~", "", t)
    t = re.sub(r"(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)", "", t)  # *italic*
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # headings
    t = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", t, flags=re.M)  # list bullets
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    joined = ""
    for ln in lines:  # join lines, adding "; " where a line didn't end a sentence
        if joined:
            joined += (" " if joined[-1] in ".!?:;," else "; ")
        joined += ln
    t = re.sub(r"\s+", " ", joined).strip()
    if bot_name:
        t = re.sub(r"^" + re.escape(bot_name) + r"\s*:\s*", "", t, flags=re.I)
    return t


def part_budget(packet_text_bytes: int, bot_name: str, n_parts: int, prefix: bool = True) -> int:
    """Text bytes per part. prefix=False for DMs, which carry no "Name: " in the text."""
    b = packet_text_bytes - (blen(bot_name) + 2 if prefix else 0)
    if n_parts > 1:
        b -= blen(f" {n_parts}/{n_parts}")
    return b


def split_text(text: str, budget: int) -> list[str]:
    """Greedy split on sentence boundaries, then words, then raw bytes."""
    if budget < 8:
        raise ValueError("packet budget too small")
    chunks: list[str] = []
    cur = ""

    def push(piece: str):
        nonlocal cur
        if not cur:
            cur = piece
        elif blen(cur) + 1 + blen(piece) <= budget:
            cur += " " + piece
        else:
            chunks.append(cur)
            cur = piece

    for sentence in re.split(r"(?<=[.!?;])\s+", text.strip()):
        if not sentence:
            continue
        if blen(sentence) <= budget:
            push(sentence)
            continue
        for word in sentence.split(" "):
            while blen(word) > budget:  # a monster token (URL etc.)
                head = cut_bytes(word, budget)
                push(head)
                word = word[len(head):]
            if word:
                push(word)
    if cur:
        chunks.append(cur)
    return chunks


def _shorten_last(s: str, budget: int) -> str:
    room = budget - blen(ELLIPSIS)
    if blen(s) <= room:
        return s.rstrip(" .,;") + ELLIPSIS
    head = cut_bytes(s, room)
    if " " in head[len(head) // 2:]:
        head = head[:head.rfind(" ")]
    return head.rstrip(" .,;") + ELLIPSIS


@dataclass
class Fit:
    parts: list[str] = field(default_factory=list)  # final text per packet, markers included
    overflow: bool = False   # text did not fit in max_parts
    truncated: bool = False  # ...so it was cut (only when allow_truncate)
    resplit: bool = False    # model's own split was not usable


def fit(raw_parts: list[str], bot_name: str, packet_text_bytes: int, max_parts: int,
        allow_truncate: bool = True, prefix: bool = True) -> Fit:
    parts = [p for p in (clean(x, bot_name) for x in raw_parts or []) if p]
    if not parts:
        return Fit()
    max_parts = max(1, int(max_parts))
    res = Fit()

    # 1) Keep the model's own split if it fits as-is.
    n = len(parts)
    if n <= max_parts and all(blen(p) <= part_budget(packet_text_bytes, bot_name, n, prefix) for p in parts):
        chunks = parts
    else:
        res.resplit = True
        text = " ".join(parts)
        chunks = None
        for k in range(1, max_parts + 1):
            c = split_text(text, part_budget(packet_text_bytes, bot_name, k, prefix))
            if len(c) <= k:
                chunks = c
                break
        if chunks is None:
            res.overflow = True
            if not allow_truncate:
                return res
            b = part_budget(packet_text_bytes, bot_name, max_parts, prefix)
            c = split_text(text, b)[:max_parts]
            c[-1] = _shorten_last(c[-1], b)
            chunks, res.truncated = c, True

    n = len(chunks)
    res.parts = [f"{c} {i}/{n}" if n > 1 else c for i, c in enumerate(chunks, 1)]
    return res
