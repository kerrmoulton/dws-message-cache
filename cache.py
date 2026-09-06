#!/usr/bin/env python3
"""Explicit DWS history sync and a durable, incremental analysis inbox. Stdlib only."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parent
TZ = dt.timezone(dt.timedelta(hours=8))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_time(value):
    value = dt.datetime.fromisoformat(value)
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def timestamp(value):
    return value.astimezone(TZ).isoformat(timespec="seconds")


def run_dws(args, profile):
    command = ["dws", *args, "--profile", profile, "--format", "json"]
    # Retry a read once with verbose diagnostics; never interpret a failure as empty.
    for attempt in range(2):
        try:
            result = subprocess.run(command + (["--verbose"] if attempt else []),
                                    capture_output=True, text=True, timeout=120)
            value = json.loads(result.stdout) if result.stdout.strip() else {}
            if result.returncode or not isinstance(value, dict) or value.get("success") is False or value.get("errorCode"):
                raise RuntimeError(f"DWS failed: {result.stdout}\n{result.stderr}")
            return value
        except (subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError) as exc:
            if attempt:
                raise RuntimeError(str(exc)) from exc


def validate(config):
    if not config.get("profile") or ":" not in config["profile"]:
        raise ValueError("profile must be the verified corpId:userId from dws profile list")
    names = set()
    for source in config["sources"]:
        if not source.get("id") or source["id"] in names:
            raise ValueError("Source IDs must be nonempty and unique")
        names.add(source["id"])
        if source.get("kind") not in ("conversation", "mentions"):
            raise ValueError("kind must be conversation or mentions")
        if source["kind"] == "conversation" and not source.get("conversation_id"):
            raise ValueError("Use a verified conversation_id, including for direct chats")
    if not 1 <= config.get("page_size", 30) <= 30:
        raise ValueError("page_size must be 1..30")
    if config.get("overlap_seconds", 300) < 0 or config.get("initial_days", 7) <= 0:
        raise ValueError("Invalid time window")


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS messages (
        profile TEXT NOT NULL, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
        time TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(profile, conversation_id, message_id));
      CREATE TABLE IF NOT EXISTS changes (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
        conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
        operation TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS checkpoints (source_key TEXT PRIMARY KEY, until TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS acknowledgements (profile TEXT PRIMARY KEY, seq INTEGER NOT NULL);
    """)
    return db


@contextlib.contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another cache command is running; try again when it finishes")
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def source_key(config, source):
    if source.get("_checkpoint_key"):
        return source["_checkpoint_key"]
    # Changing a target or exclusion must not inherit another target's checkpoint.
    identity = [config["profile"], source, sorted(config.get("exclude_conversation_ids", []))]
    return hashlib.sha256(dumps(identity).encode()).hexdigest()


def decode_page(value):
    if value.get("success") is False or value.get("errorCode"):
        raise RuntimeError("DWS returned an error")
    result = value.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("hasMore"), bool):
        raise RuntimeError("Unrecognized DWS response; checkpoint unchanged")
    groups = result.get("conversationMessagesList", [])
    if not isinstance(groups, list):
        raise RuntimeError("Invalid conversationMessagesList")
    # The observed empty response contains only hasMore=false and nextCursor.
    unknown = set(result) - {"conversationMessagesList", "hasMore", "nextCursor"}
    if unknown and "conversationMessagesList" not in result:
        raise RuntimeError(f"Unrecognized result fields: {sorted(unknown)}")
    return groups, result["hasMore"], result.get("nextCursor")


def normalize(group, message):
    cid = message.get("openConversationId") or group.get("openConversationId")
    mid = message.get("openMessageId")
    if not cid or not mid or not message.get("createTime") or "content" not in message:
        raise RuntimeError("Message missing ID/time/content; checkpoint unchanged")
    result = {"conversation_id": cid, "message_id": mid,
              "conversation": group.get("title", ""),
              "time": timestamp(parse_time(message["createTime"])),
              "sender": message.get("sender", ""),
              "sender_id": message.get("senderOpenDingTalkId", ""),
              "content": message["content"]}
    if "singleChat" in group:
        result["single_chat"] = group["singleChat"]
    quote = message.get("quotedMessage")
    if quote:
        result["quote"] = {key: quote[key] for key in
                           ("content", "sender", "createTime", "openMessageId") if key in quote}
    if message.get("emotionReplyList"):
        result["reactions"] = message["emotionReplyList"]
    resources = [{k: r[k] for k in ("resourceId", "resourceIdType", "resourceType") if k in r}
                 for r in message.get("resources", []) if isinstance(r, dict)]
    if resources:
        result["resources"] = resources
    # Signed download URLs are intentionally omitted; content retains media IDs.
    return result


def save_message(db, profile, message):
    key = (profile, message["conversation_id"], message["message_id"])
    payload = dumps(message)
    old = db.execute("SELECT payload FROM messages WHERE profile=? AND conversation_id=? AND message_id=?", key).fetchone()
    if old and old["payload"] == payload:
        return "unchanged"
    operation = "updated" if old else "inserted"
    db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)", (*key, message["time"], payload))
    db.execute("INSERT INTO changes(profile,conversation_id,message_id,operation,payload) VALUES (?,?,?,?,?)",
               (*key, operation, payload))
    return operation


def sync_source(db, config, source, since=None, until=None, max_pages=200, runner=run_dws, dry_run=False, observer=None):
    end = parse_time(until) if until else dt.datetime.now(TZ).replace(microsecond=0)
    key = source_key(config, source)
    previous = db.execute("SELECT until FROM checkpoints WHERE source_key=?", (key,)).fetchone()
    if since:
        start = parse_time(since)
    elif previous:
        start = parse_time(previous["until"]) - dt.timedelta(seconds=config.get("overlap_seconds", 300))
    else:
        start = end - dt.timedelta(days=config.get("initial_days", 7))
    if start > end:
        raise ValueError("since is after until")
    base = ["chat", "message", "search-advanced", "--start", timestamp(start), "--end", timestamp(end),
            "--limit", str(config.get("page_size", 30))]
    if source["kind"] == "conversation":
        base += ["--conversation-ids", source["conversation_id"]]
    else:
        base += ["--at-me"]
    stats = dict(source=source["id"], since=timestamp(start), until=timestamp(end),
                 pages=0, inserted=0, updated=0, unchanged=0, filtered=0)
    if dry_run:
        return {**stats, "command": ["dws", *base, "--cursor", "0", "--profile", config["profile"], "--format", "json"]}
    cursor, seen = "0", {"0"}
    excludes = set(config.get("exclude_conversation_ids", []))
    for _ in range(max_pages):
        groups, more, next_cursor = decode_page(runner([*base, "--cursor", cursor], config["profile"]))
        with db:
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("messages"), list):
                    raise RuntimeError("Malformed conversation group")
                for raw in group["messages"]:
                    message = normalize(group, raw)
                    if source["kind"] == "conversation" and message["conversation_id"] != source["conversation_id"]:
                        raise RuntimeError("DWS returned a conversation outside the requested filter")
                    if message["conversation_id"] in excludes or not start <= parse_time(message["time"]) <= end:
                        stats["filtered"] += 1
                        continue
                    stats[save_message(db, config["profile"], message)] += 1
                    if observer:
                        observer(message)
        stats["pages"] += 1
        if not more:
            # Advance only after every page succeeds; empty windows still advance.
            checkpoint = max(end, parse_time(previous["until"])) if previous else end
            with db:
                db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (key, timestamp(checkpoint)))
            return stats
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            raise RuntimeError("Missing/repeated pagination cursor; checkpoint unchanged")
        cursor = next_cursor
        seen.add(cursor)
    raise RuntimeError("Page limit reached; partial messages saved, checkpoint unchanged. Increase --max-pages")


def atomic_write(path, rows):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(dumps(row) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def export_cache(db, profile, directory, after=None):
    ack = db.execute("SELECT seq FROM acknowledgements WHERE profile=?", (profile,)).fetchone()
    after = (ack["seq"] if ack else 0) if after is None else after
    maximum = db.execute("SELECT COALESCE(MAX(seq),0) FROM changes WHERE profile=?", (profile,)).fetchone()[0]
    if not 0 <= after <= maximum:
        raise ValueError("after is outside the profile's change sequence")
    messages = db.execute("SELECT payload FROM messages WHERE profile=? ORDER BY time,conversation_id,message_id", (profile,))
    atomic_write(directory / "messages.jsonl", (json.loads(row[0]) for row in messages))
    changes = db.execute("""SELECT c.* FROM changes c JOIN (
      SELECT conversation_id,message_id,MAX(seq) seq FROM changes WHERE profile=? AND seq>? AND seq<=?
      GROUP BY conversation_id,message_id) latest ON c.seq=latest.seq ORDER BY c.seq""", (profile, after, maximum))
    pending = [{"change_seq": row["seq"], "operation": row["operation"], **json.loads(row["payload"])} for row in changes]
    atomic_write(directory / "pending.jsonl", pending)
    meta = dict(profile=profile, after_seq=after, through_seq=maximum, pending_messages=len(pending))
    atomic_write(directory / "pending-meta.json", [meta])
    return meta


def acknowledge(db, profile, through):
    maximum = db.execute("SELECT COALESCE(MAX(seq),0) FROM changes WHERE profile=?", (profile,)).fetchone()[0]
    old = db.execute("SELECT seq FROM acknowledgements WHERE profile=?", (profile,)).fetchone()
    if not (old[0] if old else 0) <= through <= maximum:
        raise ValueError("Acknowledgement must be monotonic and no greater than the latest change")
    with db:
        db.execute("INSERT OR REPLACE INTO acknowledgements VALUES (?,?)", (profile, through))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="One explicit history/catch-up sync, not a polling daemon")
    sync.add_argument("--source", action="append")
    sync.add_argument("--since")
    sync.add_argument("--until")
    sync.add_argument("--max-pages", type=int, default=200)
    sync.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    export = sub.add_parser("export")
    export.add_argument("--after", type=int)
    ack = sub.add_parser("ack", help="Mark changes analyzed only after the analysis was saved")
    ack.add_argument("--through", type=int, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    validate(config)
    directory = (args.config.parent / config.get("cache_dir", "cache")).resolve()
    # Isolate exports as well as database rows when using a different profile.
    directory /= hashlib.sha256(config["profile"].encode()).hexdigest()[:16]
    with locked(directory), contextlib.closing(database(directory / "messages.sqlite3")) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").fetchone():
            raise ValueError("This cache has migrated to ddmsg. Use ddmsg collect/status/export/ack so SQLite source settings remain authoritative.")
        if args.command == "sync":
            selected = args.source or [s["id"] for s in config["sources"] if s.get("enabled", True)]
            sources = {s["id"]: s for s in config["sources"]}
            if set(selected) - sources.keys() or args.max_pages < 1:
                raise ValueError("Unknown source or invalid max-pages")
            # One fixed end across sources avoids a moving pagination window.
            end = args.until or timestamp(dt.datetime.now(TZ))
            try:
                for name in selected:
                    print(dumps(sync_source(db, config, sources[name], args.since, end,
                                            args.max_pages, dry_run=args.dry_run)), flush=True)
            finally:
                if not args.dry_run:
                    print(dumps({"export": export_cache(db, config["profile"], directory), "directory": str(directory)}))
        elif args.command == "ack":
            acknowledge(db, config["profile"], args.through)
            print(dumps(export_cache(db, config["profile"], directory)))
        elif args.command == "export":
            print(dumps(export_cache(db, config["profile"], directory, args.after)))
        else:
            checkpoints = []
            for source in config["sources"]:
                row = db.execute("SELECT until FROM checkpoints WHERE source_key=?", (source_key(config, source),)).fetchone()
                checkpoints.append({"source": source["id"], "name": source.get("name"), "synced_until": row[0] if row else None})
            count = db.execute("SELECT COUNT(*) FROM messages WHERE profile=?", (config["profile"],)).fetchone()[0]
            print(dumps({"profile": config["profile"], "messages": count, "directory": str(directory), "sources": checkpoints}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
