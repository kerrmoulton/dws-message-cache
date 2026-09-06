"""One @me stream plus bounded history catch-up. Threads read pipes; SQLite stays on the main thread."""
import json
import datetime as dt
import queue
import signal
import subprocess
import threading
import time
import os

import cache


def listen(app, interval=600, duration=None):
    stop = threading.Event()
    events = queue.Queue(maxsize=256)
    overflow = threading.Event()
    previous = {}
    def request_stop(*_):
        stop.set()
        app.client.deadline = time.monotonic()
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, request_stop)
    start = time.monotonic()
    next_collect, next_event_refresh, next_restart = start, start, start
    child, threads = None, []
    ready, received, refreshes, restarts = False, 0, 0, 0
    pending = False
    followups = 0
    heartbeat = 0

    def read(pipe, channel):
        for line in iter(pipe.readline, ""):
            try:
                events.put_nowait((channel, line))
            except queue.Full:
                overflow.set()  # Next cycle backfills, rather than silently dropping gaps.
        pipe.close()

    def shutdown_child():
        nonlocal child
        if child:
            # Closing the pipe stdin is the DWS graceful shutdown contract, including Windows.
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    app.log.error("DWS stream did not exit gracefully; inspect dws event status")
            child = None

    try:
        while not stop.is_set() and (duration is None or time.monotonic() - start < duration):
            if (app.directory / "listener.stop").exists():
                break
            current = time.monotonic()
            mention_source = app.db.execute("SELECT mode FROM sources WHERE id='mentions'").fetchone()
            mention_enabled = mention_source and mention_source[0] == "permanent"
            if not mention_enabled and child is not None:
                shutdown_child()
                ready, pending = False, False
            if mention_enabled and child is None and current >= next_restart:
                child = subprocess.Popen(app.client.command(["event", "consume", "user_im_message_receive_at", "--flatten"], "ndjson"),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for channel, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
                    thread = threading.Thread(target=read, args=(pipe, channel), daemon=True)
                    thread.start()
                    threads.append(thread)
                restarts += 1
            if child is not None and child.poll() is not None:
                app.log.warning("stream exited code=%s; retry in 30s", child.returncode)
                shutdown_child()
                ready, pending = False, True
                next_restart = current + 30
            for _ in range(256):
                try:
                    channel, line = events.get_nowait()
                except queue.Empty:
                    break
                if channel == "stderr":
                    if "[event] ready" in line:
                        ready = True
                        app.log.info("DWS @me event stream ready")
                    elif "error" in line.lower():
                        app.log.warning("stream: %s", line.strip()[:1000])
                else:
                    try:
                        event = json.loads(line)
                        if event.get("type") == "user_im_message_receive_at" and event.get("message_id"):
                            received += 1
                            pending = True
                            followups = 2
                    except (ValueError, AttributeError):
                        app.log.warning("unrecognized event line; scheduling catch-up")
                        pending = True
            if overflow.is_set():
                pending = True
                overflow.clear()
            if current >= next_collect or mention_enabled and pending and current >= next_event_refresh:
                all_sources = current >= next_collect
                try:
                    budget = 180 if all_sources else 90
                    if duration is not None:
                        budget = max(1, min(budget, duration-(time.monotonic()-start)))
                    result = app.collect(None if all_sources else ["mentions"], max_seconds=budget)
                    refreshes += 1
                    if result["errors"]:
                        app.log.warning("background sync incomplete: %s", cache.dumps(result["errors"])[:2000])
                    followups = max(0, followups-1)
                    pending = bool(result["errors"]) or followups > 0
                except (RuntimeError, OSError, ValueError) as exc:
                    app.log.warning("background sync failed: %s", str(exc)[:2000])
                    pending = True
                if all_sources:
                    next_collect = time.monotonic() + interval
                # Batch bursts, allow indexing lag, cap at one @ refresh per 20 seconds.
                next_event_refresh = time.monotonic() + 20
            if current >= heartbeat:
                with app.db:
                    app.store.set("listener", {"pid": os.getpid(), "ready": ready, "received_events": received,
                        "heartbeat": cache.timestamp(dt.datetime.now(cache.TZ)), "refreshes": refreshes})
                heartbeat = time.monotonic() + 15
            stop.wait(0.25)
        return {"stream_ready": ready, "received_events": received, "refreshes": refreshes, "starts": restarts,
                "seconds": round(time.monotonic()-start, 2)}
    finally:
        shutdown_child()
        for thread in threads:
            thread.join(timeout=1)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        with app.db:
            app.store.set("listener", {"ready": False, "stopped": True})
