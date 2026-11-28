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


MORE_HINT = " (reply more)"


def _markers(chunks: list[str]) -> list[str]:
    n = len(chunks)
    return [f"{c} {i}/{n}" if n > 1 else c for i, c in enumerate(chunks, 1)]


def _take(text: str, budget: int, n: int, last_less: int = 0) -> tuple[list[str], str]:
    """Up to n packet texts from the front of `text`, each within budget (the last within budget - last_less).
    Packets end at a list item boundary (", " or "; ") when one is in the second half, so names stay whole."""
    chunks = []
    for i in range(n):
        if not text:
            break
        b = budget - (last_less if i == n - 1 else 0)
        head = split_text(text, b)[0]
        if not text.startswith(head):  # split_text re-joined a cut token; fall back to raw bytes
            head = cut_bytes(text, b)
        if len(head) < len(text) and not head.endswith((",", ";", ".", ":")):
            cut = max(head.rfind(", "), head.rfind("; "))
            if cut > len(head) // 2:
                head = head[:cut + 1]
        chunks.append(head)
        text = text[len(head):].lstrip()
    return chunks, text


def paginate(raw_parts: list[str], bot_name: str, packet_text_bytes: int, max_parts: int, max_batches: int,
             prefix: bool = True, hint: str = MORE_HINT) -> list[list[str]]:
    """Split a long answer into numbered packets ("1/8" ... "8/8", counted over the whole answer) and group
    them into batches of max_parts: the first batch goes out at once, each "more" sends the next. The last
    packet of every batch but the final one ends with `hint`. Past max_batches the answer is cut with "..."."""
    pieces = [p for p in (clean(x, bot_name) for x in raw_parts or []) if p]
    # The model often splits a list over several strings; keep the list's separator where they join.
    sep = "; " if sum(p.count("; ") for p in pieces) >= sum(p.count(", ") for p in pieces) else ", "
    text = ""
    for p in pieces:
        text += ("" if not text else " " if text[-1] in ".!?;:," else sep) + p
    max_parts, max_batches = max(1, int(max_parts)), max(1, int(max_batches))
    if not text:
        return []
    if blen(text) <= part_budget(packet_text_bytes, bot_name, 1, prefix):
        return [[text]]
    cap = max_parts * max_batches
    b = part_budget(packet_text_bytes, bot_name, 1, prefix) - blen(f" {cap}/{cap}")
    batches: list[list[str]] = []
    while text:
        if len(batches) == max_batches - 1:  # last batch allowed: no hint, cut what doesn't fit
            chunks, rest = _take(text, b, max_parts)
            if rest:
                chunks[-1] = _shorten_last(chunks[-1], b)
            batches.append(chunks)
            break
        chunks, rest = _take(text, b, max_parts, blen(hint))
        if rest:
            batches.append(chunks)
            text = rest
            continue
        # Everything left fits in this batch; take it again without reserving room for the hint.
        batches.append(_take(text, b, max_parts)[0])
        break
    total = sum(len(c) for c in batches)
    out, i = [], 0
    for n, chunks in enumerate(batches):
        batch = []
        for c in chunks:
            i += 1
            batch.append(f"{c} {i}/{total}")
        if n < len(batches) - 1:
            batch[-1] += hint
        out.append(batch)
    return out
