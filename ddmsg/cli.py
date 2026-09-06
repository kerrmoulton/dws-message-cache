import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import cache
from . import __version__, media, scheduler
from .app import App
from .dws import Client, executable
from .listener import listen


def bounded(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be {low}..{high}")
        return number
    return parse


def default_config():
    explicit = os.environ.get("DDMSG_CONFIG")
    if explicit:
        return Path(explicit)
    prototype = Path(__file__).resolve().parent.parent / "config.json"
    return prototype if prototype.exists() else Path.home()/".ddmsg"/"config.json"


def parser():
    p = argparse.ArgumentParser(prog="ddmsg", description="Local, bounded DingTalk work context for AI. JSON output; no model calls.")
    p.add_argument("--config", type=Path, default=default_config())
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Verify DWS identity and migrate prototype cache without deleting data")
    init.add_argument("--profile", help="Verified DWS corpId:userId; default current profile")
    sub.add_parser("status")
    sub.add_parser("schema", help="Machine-readable CLI arguments; no DWS or cache access")
    sub.add_parser("doctor", help="Check DWS capabilities, identity, local SQLite, and platform")
    discover = sub.add_parser("discover", help="Find cached or remote conversations; person means a direct-chat conversation")
    discover.add_argument("query", nargs="?", default="")
    discover.add_argument("--kind", choices=["conversation", "group", "person"], default="conversation")
    discover.add_argument("--fresh", action="store_true")
    discover.add_argument("--limit", type=bounded(1, 100), default=20)
    source = sub.add_parser("source").add_subparsers(dest="action", required=True)
    source.add_parser("list")
    for operation in ("add", "remove", "ignore"):
        s = source.add_parser(operation)
        s.add_argument("conversation", help="Exact conversation/source ID or unambiguous cached name")
        if operation == "add":
            s.add_argument("--mode", choices=["permanent", "temporary", "adhoc"], default="permanent")
            s.add_argument("--hours", type=bounded(1, 720), default=72)
            s.add_argument("--sync", action="store_true")
    settings = sub.add_parser("settings").add_subparsers(dest="action", required=True)
    settings.add_parser("show")
    setp = settings.add_parser("set")
    setp.add_argument("key", choices=["initial_days", "overlap_seconds", "temporary_hours", "max_auto_sources"])
    setp.add_argument("value", type=int)
    collect = sub.add_parser("collect", aliases=["sync"], help="Bounded incremental collection, no JSONL rewrite or AI call")
    collect.add_argument("--source", action="append")
    collect.add_argument("--since")
    collect.add_argument("--until")
    collect.add_argument("--max-seconds", type=bounded(10, 3600), default=180)
    collect.add_argument("--max-pages", type=bounded(1, 2000), default=200)
    for name in ("query", "recent", "at-me"):
        q = sub.add_parser(name)
        q.add_argument("--conversation", "--source", dest="conversation")
        q.add_argument("--sender")
        q.add_argument("--text")
        q.add_argument("--since")
        q.add_argument("--until")
        q.add_argument("--limit", type=bounded(1, 100), default=20)
        q.add_argument("--before", type=int, help="Local ref returned in next_before, for older pages")
        q.add_argument("--max-chars", type=bounded(100, 10000), default=1200)
        q.add_argument("--fresh", action="store_true", help="Refresh only this conversation (at-me refreshes mention source)")
        q.add_argument("--include-ignored", action="store_true")
        if name == "at-me":
            q.add_argument("--kind", choices=["direct", "all", "unknown"], default="direct")
    ctx = sub.add_parser("context")
    ctx.add_argument("ref", type=int)
    ctx.add_argument("--before", type=bounded(0, 30), default=15)
    ctx.add_argument("--after", type=bounded(0, 30), default=10)
    ctx.add_argument("--fresh", action="store_true")
    ms = sub.add_parser("media").add_subparsers(dest="action", required=True)
    ml = ms.add_parser("list")
    ml.add_argument("--message", type=int)
    md = ms.add_parser("get")
    md.add_argument("id", type=int)
    ma = ms.add_parser("annotate", help="Cache already-produced image analysis; does not call AI")
    ma.add_argument("id", type=int)
    ma.add_argument("--file", type=Path, required=True)
    ma.add_argument("--model", required=True)
    inbox = sub.add_parser("inbox")
    inbox.add_argument("--limit", type=bounded(1, 30), default=20)
    ack = sub.add_parser("ack")
    ack.add_argument("batch")
    sub.add_parser("export", help="Explicitly regenerate full and pending JSONL exports")
    ls = sub.add_parser("listen", help="One @me event stream plus periodic history catch-up")
    ls.add_argument("--interval", type=bounded(60, 86400), default=600)
    ls.add_argument("--duration", type=bounded(1, 86400), help="Optional bounded trial in seconds")
    schedule = sub.add_parser("scheduler").add_subparsers(dest="action", required=True)
    si = schedule.add_parser("install")
    si.add_argument("--mode", choices=["hybrid", "interval"], default="hybrid")
    si.add_argument("--interval", type=bounded(60, 86400), default=600)
    si.add_argument("--dry-run", action="store_true")
    si.add_argument("--platform", choices=["darwin", "win32"], help="Only for dry-run portability inspection")
    schedule.add_parser("status")
    schedule.add_parser("run-now")
    su = schedule.add_parser("uninstall")
    su.add_argument("--dry-run", action="store_true")
    return p


def initialize(path, profile):
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        if profile and profile != config["profile"]:
            raise ValueError("Use a separate --config path for another account; do not change this cache's identity")
    else:
        result = subprocess.run([executable(), "profile", "list", "--format", "json"], capture_output=True, text=True, encoding="utf-8", timeout=45)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        profiles = json.loads(result.stdout)
        selected = profile or profiles.get("currentProfile")
        if not any(row.get("profile") == selected for row in profiles.get("profiles", [])):
            raise ValueError("Select an existing corpId:userId from dws profile list")
        config = {"profile": selected, "cache_dir": "cache", "sources": [{"id": "mentions", "name": "@我", "kind": "mentions"}]}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.dumps(config), encoding="utf-8")
    app = App(path)
    try:
        return {"identity": app.identity(), "status": app.status()}
    finally:
        app.close()


def execute(args, app):
    cmd = args.command
    if cmd == "status":
        return app.status()
    if cmd == "doctor":
        version = subprocess.run([executable(), "--version", "--format", "json"], capture_output=True, text=True, timeout=10)
        required = {"search-advanced": ["chat", "message", "search-advanced"], "download-media": ["chat", "message", "download-media"],
                    "events": ["event", "consume"], "conversations": ["chat", "list-all-conversations"]}
        capabilities = {}
        for key, argv in required.items():
            p = subprocess.run([executable(), *argv, "--help", "--format", "json"], capture_output=True, text=True, timeout=10)
            capabilities[key] = p.returncode == 0
        return {"dws": executable(), "version": version.stdout.strip(), "capabilities": capabilities,
                "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "fts5_trigram": app.store.fts,
                "platform": sys.platform, "identity": app.identity()}
    if cmd == "discover":
        return app.discover(args.query, args.kind, args.fresh, args.limit)
    if cmd == "source":
        if args.action == "list":
            return {"sources": [app.public_source(r) for r in app.store.source_rows()]}
        mode = args.mode if args.action == "add" else "disabled" if args.action == "remove" else "ignored"
        source = app.add_source(args.conversation, mode, getattr(args, "hours", 72))
        result = {"source": app.public_source(source)}
        if getattr(args, "sync", False):
            result["sync"] = app.collect([source["id"]])
        return result
    if cmd == "settings":
        keys = {"initial_days": (1, 365), "overlap_seconds": (0, 86400), "temporary_hours": (1, 720), "max_auto_sources": (0, 100)}
        if args.action == "set":
            low, high = keys[args.key]
            if not low <= args.value <= high:
                raise ValueError(f"{args.key} must be {low}..{high}")
            with cache.locked(app.directory), app.db:
                app.store.set(args.key, args.value)
        return {key: app.store.get(key) for key in keys}
    if cmd in ("collect", "sync"):
        return app.collect(args.source, args.since, args.until, args.max_seconds, args.max_pages)
    if cmd in ("query", "recent", "at-me"):
        refresh = None
        if args.fresh:
            if cmd != "at-me" and not args.conversation:
                raise ValueError("--fresh requires --conversation; use collect for all sources")
            refresh = app.collect(["mentions"] if cmd == "at-me" else [args.conversation])
        result = app.query(args.conversation, args.sender, args.text, args.since, args.until,
                           args.kind if cmd == "at-me" else None, args.limit, args.before, args.include_ignored, args.max_chars)
        if refresh is not None:
            result["refresh"] = refresh
        return result
    if cmd == "context":
        return app.context(args.ref, args.before, args.after, args.fresh)
    if cmd == "media":
        if args.action == "list":
            return media.list_media(app, args.message)
        if args.action == "get":
            return media.download(app, args.id)
        return media.annotate(app, args.id, args.file, args.model)
    if cmd == "inbox":
        return app.inbox(args.limit)
    if cmd == "ack":
        return app.ack(args.batch)
    if cmd == "export":
        with cache.locked(app.directory):
            return cache.export_cache(app.db, app.profile, app.directory)
    if cmd == "listen":
        with cache.locked(app.directory / "listener"):
            return listen(app, args.interval, args.duration)
    if cmd == "scheduler":
        target = getattr(args, "platform", None)
        if target and not args.dry_run:
            raise ValueError("--platform is for --dry-run only")
        return scheduler.operate(app, args.action, getattr(args, "mode", "hybrid"), getattr(args, "interval", 600),
                                 getattr(args, "dry_run", False), target)
    raise ValueError("Unknown command")


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    app = None
    try:
        if args.command == "schema":
            def describe(p):
                flags, commands = [], {}
                for a in p._actions:
                    if isinstance(a, argparse._SubParsersAction):
                        commands = {name: describe(child) for name, child in a.choices.items()}
                    elif a.dest != "help":
                        flags.append({"name": a.dest, "flags": a.option_strings, "required": a.required,
                                      "choices": a.choices, "help": a.help})
                return {"arguments": flags, "commands": commands}
            result = {"version": __version__, "cli": describe(parser()), "output": "JSON", "exit_codes": {"0": "success", "1": "failure", "2": "partial sync/refresh failure"}}
        elif args.command == "init":
            result = initialize(args.config.resolve(), args.profile)
        else:
            if not args.config.exists():
                raise ValueError("No configuration yet. Run ddmsg init --profile <corpId:userId>")
            app = App(args.config)
            saved = app.store.get("dws_binary")
            if saved and Path(saved).is_file():
                app.client.binary = saved
            result = execute(args, app)
        if sys.stdout is not None:
            print(cache.dumps({"schema_version": 1, "ok": True, "result": result}))
        if isinstance(result, dict) and result.get("errors"):
            return 2
        if isinstance(result, dict) and isinstance(result.get("refresh"), dict) and result["refresh"].get("errors"):
            return 2
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        if sys.stdout is not None:
            print(cache.dumps({"schema_version": 1, "ok": False, "error": str(exc)}))
        return 1
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    sys.exit(main())
