"""Re-run logged decisions against the CURRENT prompts/config (the web UI's Decisions tab does the same).

  docker compose exec meshbot python -m app.replay --last 20 --changed
  docker compose exec meshbot python -m app.replay --thread "Alice: anyone know how far LoRa reaches?"

Nothing is transmitted.
"""
from __future__ import annotations

import argparse
import asyncio

from .config import Config
from .main import Bot


async def main_async(args) -> None:
    bot = Bot(Config())
    changed = 0
    if args.thread:
        jobs = [(None, args.channel or "#testbot",
                 [{"sender": l.partition(": ")[0], "text": l.partition(": ")[2]} for l in args.thread])]
    else:
        recs = bot.history.decisions(args.channel, None, args.last)
        jobs = [(r, r["channel"], r["thread"]) for r in reversed(recs)]
    for old, channel, thread in jobs:
        new = await bot.playground(channel, thread)
        diff = old is not None and bool(old.get("respond")) != new["respond"]
        changed += diff
        if args.changed and not diff:
            continue
        last = thread[-1]
        print(f"\n=== {old['time'] if old else 'ad hoc'} {channel}  {last['sender']}: {last['text']}")
        if old:
            print(f"  OLD {old.get('outcome')}  reason={old.get('reason')!r}")
            for p in old.get("parts") or []:
                print(f"      | {p}")
        print(f"  NEW respond={new['respond']}  reason={new['reason']!r}  ({new['latency_s']}s)"
              + (f"  ERROR {new['error']}" if new["error"] else ""))
        for p in new.get("parts") or []:
            print(f"      | {p}")
    await bot.brain.close()
    print(f"\n{len(jobs)} replayed, {changed} changed decision")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--last", type=int, default=20, help="the last N decisions (default 20)")
    ap.add_argument("--channel", help="only this channel, e.g. #testbot")
    ap.add_argument("--changed", action="store_true", help="print only decisions that flip")
    ap.add_argument("--thread", nargs="+", help='ad-hoc thread lines "Sender: text" (newest last)')
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
