"""Bot usage log: who asked, what was said and in which voice.

Every command becomes one JSON line in logs/history.jsonl (JSON Lines). One
line per event, always appended at the end — text with commas, quotes or
emoji doesn't break the file, and the history can be read while the bot runs.

Reports:

    python history.py summary
        Totals by person, by voice and by day.

    python history.py show -n 20
        The latest utterances, one per line.

    python history.py csv -o history.csv
        Exports to CSV (opens in Excel with accents intact).

All of them take --since 2026-08-01 and --until 2026-08-31 to narrow the period.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent


def history_file() -> Path:
    """Where the history lives. It can point to another disk through the .env.

    Resolved on every call because the bot imports this module before loading
    the .env — reading the variable on import would get the wrong value.
    """
    return Path(os.getenv("HISTORY_FILE", ROOT / "logs" / "history.jsonl"))


CSV_COLUMNS = [
    "time",
    "event",
    "user",
    "user_id",
    "nickname",
    "voice",
    "text",
    "chars",
    "guild",
    "voice_channel",
]


# ------------------------------------------------------------------- writing


def record(event: str, **fields: Any) -> None:
    """Appends an event to the history.

    Never raises: losing a log line can't take down a bot command halfway
    through.
    """
    line = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **fields}
    try:
        dest = history_file()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - the log is secondary, it can't break the bot
        import logging

        logging.getLogger("tts-bot").exception("Failed to write the history")


def record_interaction(event: str, interaction: Any, **fields: Any) -> None:
    """Pulls who/where out of a discord.Interaction and records the event."""
    user = interaction.user
    guild = interaction.guild
    voice_channel = getattr(getattr(user, "voice", None), "channel", None)

    record(
        event,
        user=str(user),
        user_id=getattr(user, "id", None),
        nickname=getattr(user, "display_name", None),
        guild=getattr(guild, "name", None),
        guild_id=getattr(guild, "id", None),
        voice_channel=getattr(voice_channel, "name", None),
        **fields,
    )


# ------------------------------------------------------------------- reading


def read(
    since: str | None = None,
    until: str | None = None,
    guild_id: int | None = None,
) -> list[dict[str, Any]]:
    """Reads the whole history, optionally narrowed by date (YYYY-MM-DD).

    `guild_id` limits it to one server — it's what /status uses so it doesn't
    leak what was said on another Discord.
    """
    source = history_file()
    if not source.is_file():
        return []

    def inside(entry: dict[str, Any]) -> bool:
        day = str(entry.get("time", ""))[:10]
        if since and day < since:
            return False
        if until and day > until:
            return False
        if guild_id is not None and entry.get("guild_id") != guild_id:
            return False
        return True

    records = []
    with source.open(encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(f"  (line {number} is corrupted, skipped)", file=sys.stderr)
                continue
            if inside(entry):
                records.append(entry)
    return records


def utterances(records: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Only the /say requests — drops /stop, /skip, /leave and the like."""
    return (r for r in records if r.get("event") == "say")


def stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary of the records. The CLI and the bot's /status read from here.

    A zero `total` means there were no utterances in the range; the other
    fields come back empty and the caller decides what to show.
    """
    said = list(utterances(records))
    total = len(said)
    if not total:
        return {"total": 0, "events": len(records), "by_user": Counter(),
                "by_voice": Counter(), "by_day": Counter(), "others": Counter()}

    days = sorted(str(u.get("time", ""))[:10] for u in said)
    chars = sum(int(u.get("chars") or 0) for u in said)

    return {
        "total": total,
        "events": len(records),
        "chars": chars,
        "avg_chars": chars / total,
        "first_day": days[0],
        "last_day": days[-1],
        # By username, not nickname: the nickname changes per server and
        # would split the same person into two lines.
        "by_user": Counter(u.get("user") for u in said),
        "by_voice": Counter(u.get("voice") for u in said),
        "by_day": Counter(days),
        "others": Counter(
            r.get("event") for r in records if r.get("event") != "say"
        ),
    }


# ------------------------------------------------------------------ commands


def _table(title: str, counts: Counter, total: int) -> None:
    if not counts:
        return
    print(f"\n{title}")
    width = max(len(str(k)) for k in counts)
    for key, n in counts.most_common():
        pct = 100 * n / total if total else 0
        print(f"  {str(key):<{width}}  {n:>5}  {pct:5.1f}%")


def cmd_summary(since: str | None, until: str | None) -> int:
    records = read(since, until)
    if not records:
        print(f"nothing in {history_file()}")
        return 1

    s = stats(records)
    total = s["total"]
    if not total:
        print(f"{s['events']} event(s), no utterances.")
        return 0

    print(f"{total} utterance(s) from {s['first_day']} to {s['last_day']}")
    print(f"{s['chars']} characters in total, "
          f"{s['avg_chars']:.0f} per utterance on average")

    _table("By person:", s["by_user"], total)
    _table("By voice:", s["by_voice"], total)
    _table("By day:", s["by_day"], total)
    _table("Other events:", s["others"], sum(s["others"].values()))
    return 0


def cmd_show(n: int, since: str | None, until: str | None) -> int:
    records = read(since, until)
    said = list(utterances(records))
    if not said:
        print(f"nothing in {history_file()}")
        return 1

    for u in said[-n:]:
        when = str(u.get("time", ""))[:19].replace("T", " ")
        print(f"{when}  {u.get('user')}  [{u.get('voice')}]  {u.get('text')}")
    return 0


def cmd_csv(output: str, since: str | None, until: str | None) -> int:
    records = read(since, until)
    if not records:
        print(f"nothing in {history_file()}")
        return 1

    dest = Path(output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: without the BOM, Excel on Windows mangles the accents.
    with dest.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    print(f"-> {dest}  ({len(records)} line(s))")
    return 0


# ---------------------------------------------------------------------- main


def main() -> int:
    # The Windows console defaults to cp1252: without this, a line with an
    # emoji crashes `show` with UnicodeEncodeError and accents come out wrong.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", help="start date, YYYY-MM-DD")
    ap.add_argument("--until", help="end date, YYYY-MM-DD")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("summary", help="totals by person, voice and day")

    s = sub.add_parser("show", help="latest utterances")
    s.add_argument("-n", type=int, default=20, help="how many to show (default 20)")

    c = sub.add_parser("csv", help="exports to CSV")
    c.add_argument("-o", "--output", default="history.csv", help="output file")

    args = ap.parse_args()

    if args.cmd == "show":
        return cmd_show(args.n, args.since, args.until)
    if args.cmd == "csv":
        return cmd_csv(args.output, args.since, args.until)
    return cmd_summary(args.since, args.until)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    sys.exit(main())
