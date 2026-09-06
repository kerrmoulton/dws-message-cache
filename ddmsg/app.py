import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import uuid

import cache
from .dws import Client
from .store import Store, now


class CrossProcessRotatingFileHandler(RotatingFileHandler):
    """Rotate safely when a listener and short-lived CLI commands share a log."""

    def __init__(self, filename, lock_directory, **kwargs):
        self.lock_directory = Path(lock_directory)
        super().__init__(filename, delay=True, **kwargs)

    def emit(self, record):
        for attempt in range(40):
            try:
                with cache.locked(self.lock_directory):
                    try:
                        super().emit(record)
                    finally:
                        # Windows cannot rename a file held open by another
                        # process. All ddmsg writers release it after each row.
                        if self.stream:
                            self.stream.close()
                            self.stream = None
                return
            except RuntimeError:
                if attempt == 39:
                    self.handleError(record)
                    return
                time.sleep(0.025)


class App:
    @staticmethod
    def public_source(row):
        return {key: row[key] for key in ("id", "kind", "cid", "name", "mode", "expires_at", "reason", "watermark")}

    def __init__(self, config_path, client=None):
        self.config_path = Path(config_path).resolve()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        cache.validate(config)
        self.profile = config["profile"]
        directory = self.config_path.parent / config.get("cache_dir", "cache") / hashlib.sha256(self.profile.encode()).hexdigest()[:16]
        self.store = Store(directory, self.profile)
        self.directory, self.db = self.store.directory, self.store.db
        if not self.store.get("migration_v2"):
            with cache.locked(self.directory):
                self.store.migrate(config)
        self.client = client or Client(self.profile)
        self.log = logging.getLogger("ddmsg." + str(self.directory))
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        if not self.log.handlers:
            handler = CrossProcessRotatingFileHandler(self.directory / "collector.log", self.directory / "log-lock",
                maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.log.addHandler(handler)

    def close(self):
        self.store.close()
        for handler in list(self.log.handlers):
            handler.close()
            self.log.removeHandler(handler)

    def identity(self):
        value = self.client(["profile", "list"])
        match = [p for p in value.get("profiles", []) if p.get("profile") == self.profile]
        if len(match) != 1:
            raise ValueError("Configured profile is not an available DWS account; use dws auth login")
        p = match[0]
        identity = {"profile": self.profile, "user_id": p.get("userId"), "name": p.get("userName"),
                    "corp_name": p.get("corpName"), "mention_names": [p["userName"]] if p.get("userName") else []}
        with self.db:
            self.store.set("identity", identity)
        return identity

    def discover(self, query="", kind="conversation", fresh=False, limit=20):
        remote_count = 0
        if fresh and kind == "person" and query:
            people = self.client(["aisearch", "person", "--keyword", query, "--dimension", "name"]).get("result")
            if not isinstance(people, list):
                raise RuntimeError("Unrecognized DWS person search result")
            if len(people) > 30:
                raise ValueError("More than 30 people match; use a more specific name")
            for person in people:
                person_id = person.get("openDingTalkId")
                user_id = person.get("userId")
                if not person_id and not user_id:
                    raise RuntimeError("DWS person result has no usable identity")
                flag, value = ("--open-dingtalk-id", person_id) if person_id else ("--user", user_id)
                info = self.client(["chat", "conversation-info", flag, value]).get("result", {}).get("conversationInfo")
                if not isinstance(info, dict) or not info.get("openConversationId"):
                    raise RuntimeError("Cannot resolve this person's direct conversation")
                with self.db:
                    self.store.conversation(info["openConversationId"], info.get("title") or person.get("author", ""), True)
                    if person_id:
                        self.db.execute("INSERT OR REPLACE INTO contacts VALUES (?,?,?)", (person_id, person.get("author", ""), now()))
                remote_count += 1
        elif fresh:
            args = ["chat", "search", "--query", query] if kind == "group" else ["chat", "list-all-conversations"]
            cursor, seen = "0", {"0"}
            for _ in range(100):
                result = self.client(args + ["--limit", "30", "--cursor", cursor]).get("result")
                field = "groups" if kind == "group" else "conversations"
                if not isinstance(result, dict) or not isinstance(result.get(field), list):
                    raise RuntimeError("Unrecognized DWS discovery result")
                with self.db:
                    for c in result[field]:
                        if not c.get("openConversationId"):
                            raise RuntimeError("DWS discovery returned no conversation ID")
                        self.store.conversation(c["openConversationId"], c.get("title", ""), False if kind == "group" else c.get("singleChat"))
                        remote_count += 1
                if not result.get("hasMore", False):
                    break
                nxt = result.get("nextCursor")
                if nxt is None or str(nxt) in seen:
                    raise RuntimeError("Discovery cursor missing or repeated")
                cursor = str(nxt)
                seen.add(cursor)
            else:
                raise RuntimeError("Discovery page limit exceeded; try a narrower group search")
        clause = " AND single_chat=0" if kind == "group" else " AND single_chat=1" if kind == "person" else ""
        rows = self.db.execute("SELECT * FROM conversations WHERE instr(lower(name),lower(?))>0" + clause + " ORDER BY name,cid LIMIT ?", (query, limit + 1)).fetchall()
        return {"items": [dict(r) for r in rows[:limit]], "has_more_local": len(rows) > limit,
                "remote_fetched": remote_count, "note": "Names may be ambiguous; use cid for mutations. Remote discovery is limited to DWS-visible results."}

    def add_source(self, selector, mode, hours=72):
        with cache.locked(self.directory), self.db:
            special = self.db.execute("SELECT * FROM sources WHERE id=? AND kind='mentions'", (selector,)).fetchone()
            if special:
                if mode == "temporary":
                    raise ValueError("Mention feed supports permanent, adhoc, disabled, or ignored")
                self.db.execute("UPDATE sources SET mode=?,updated_at=? WHERE id=?", (mode, now(), selector))
                return next(r for r in self.store.source_rows() if r["id"] == selector)
            cid = self.store.resolve(selector)["cid"]
            sid = self.store.source(cid, mode, ttl=hours)
        return next(r for r in self.store.source_rows() if r["id"] == sid)

    def runtime_source(self, row):
        return {"id": row["id"], "name": row["name"], "kind": row["kind"],
                "conversation_id": row["cid"], "_checkpoint_key": row["checkpoint_key"]}

    def sync_one(self, row, since=None, until=None, max_pages=200):
        previous = self.db.execute("SELECT until FROM checkpoints WHERE source_key=?", (row["checkpoint_key"],)).fetchone()
        return cache.sync_source(self.db, self.store.runtime_config(), self.runtime_source(row),
            since=since or (row.get("initial_since") if not previous else None), until=until, max_pages=max_pages,
            runner=self.client, observer=lambda m: self.store.index(m, at_result=row["kind"] == "mentions"))

    def collect(self, sources=None, since=None, until=None, max_seconds=180, max_pages=200, contexts=True):
        started, start_clock = now(), time.monotonic()
        stats, errors, added = [], [], []
        self.client.deadline = start_clock + max_seconds
        try:
            with cache.locked(self.directory):
                if not self.store.get("identity"):
                    self.identity()
                rows = self.store.source_rows()
                if sources:
                    chosen = []
                    for selector in sources:
                        direct = [r for r in rows if r["id"] == selector]
                        if direct:
                            chosen.extend(direct)
                        else:
                            cid = self.store.resolve(selector)["cid"]
                            row = next((r for r in rows if r["cid"] == cid), None)
                            if row is None:
                                with self.db:
                                    self.store.source(cid, "adhoc", reason="one-time targeted refresh")
                                row = next(r for r in self.store.source_rows() if r["cid"] == cid)
                            chosen.append(row)
                    rows = chosen
                else:
                    rows = [r for r in rows if r["mode"] == "permanent" or
                            r["mode"] == "temporary" and r["expires_at"] > started]
                rows = sorted({r["id"]: r for r in rows}.values(), key=lambda r: (r["kind"] != "mentions", r["watermark"] or "", r["id"]))
                for row in rows:
                    if row["mode"] == "ignored":
                        errors.append({"source": row["id"], "error": "Source is ignored; explicitly re-add before refreshing"})
                        continue
                    if time.monotonic() >= self.client.deadline:
                        errors.append({"source": row["id"], "error": "time budget exhausted"})
                        break
                    try:
                        stats.append(self.sync_one(row, since, until or started, max_pages))
                    except (RuntimeError, ValueError, OSError) as exc:
                        errors.append({"source": row["id"], "error": str(exc)[:2000]})
                with self.db:
                    added = self.store.automatic_mentions()
                if contexts:
                    for job in self.db.execute("SELECT * FROM context_jobs ORDER BY attempts,cid LIMIT 5").fetchall():
                        if time.monotonic() >= self.client.deadline:
                            break
                        cid = job["cid"]
                        blocked = self.db.execute("SELECT mode FROM sources WHERE cid=?", (cid,)).fetchone()
                        if blocked and blocked[0] in ("ignored", "disabled"):
                            with self.db:
                                self.db.execute("DELETE FROM context_jobs WHERE cid=?", (cid,))
                            continue
                        conv = self.store.resolve(cid)
                        row = {"id": "context-" + cid, "name": conv["name"], "kind": "conversation", "cid": cid,
                               "checkpoint_key": "context-" + cid}
                        try:
                            stats.append(self.sync_one(row, job["since"], min(job["until"], started), min(10, max_pages)))
                            with self.db:
                                if job["until"] <= started:
                                    self.db.execute("DELETE FROM context_jobs WHERE cid=?", (cid,))
                                else:
                                    self.db.execute("UPDATE context_jobs SET since=? WHERE cid=?", (started, cid))
                        except (RuntimeError, ValueError, OSError) as exc:
                            errors.append({"source": row["id"], "error": str(exc)[:2000]})
                            with self.db:
                                self.db.execute("UPDATE context_jobs SET attempts=attempts+1,last_error=? WHERE cid=?", (str(exc)[:1000], cid))
                result = {"started_at": started, "finished_at": now(), "seconds": round(time.monotonic() - start_clock, 2),
                          "sources": stats, "temporary_added": added, "errors": errors,
                          "inserted": sum(s["inserted"] for s in stats), "updated": sum(s["updated"] for s in stats)}
                with self.db:
                    self.db.execute("INSERT INTO runs(started_at,finished_at,result) VALUES (?,?,?)", (started, now(), cache.dumps(result)))
                    self.db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 100)")
                self.log.info("collect inserted=%s updated=%s errors=%s seconds=%s", result["inserted"], result["updated"], len(errors), result["seconds"])
                return result
        finally:
            self.client.deadline = None

    def query(self, conversation=None, sender=None, text=None, since=None, until=None,
              mention=None, limit=20, before=None, include_ignored=False, max_chars=1200):
        clauses, params = ["i.profile=?"], [self.profile]
        if conversation:
            clauses.append("i.cid=?")
            params.append(self.store.resolve(conversation)["cid"])
        if not include_ignored:
            clauses.append("NOT EXISTS (SELECT 1 FROM sources s WHERE s.cid=i.cid AND s.mode='ignored')")
        if sender:
            clauses.append("(i.sender=? OR i.sender_id=?)")
            params += [sender, sender]
        if since:
            clauses.append("i.time>=?")
            params.append(cache.timestamp(cache.parse_time(since)))
        if until:
            clauses.append("i.time<=?")
            params.append(cache.timestamp(cache.parse_time(until)))
        if mention:
            clauses.append("i.mention_kind=?")
            params.append(mention)
        if text:
            if self.store.fts and len(text) >= 3:
                clauses.append("i.id IN (SELECT rowid FROM message_fts WHERE message_fts MATCH ?)")
                params.append('"' + text.replace('"', '""') + '"')
            else:
                clauses.append("instr(lower(i.content),lower(?))>0")
                params.append(text)
        if before:
            anchor = self.store.message(before)
            clauses.append("(i.time<? OR (i.time=? AND i.id<?))")
            params += [anchor["time"], anchor["time"], before]
        sql = "SELECT i.*,c.name FROM message_index i JOIN conversations c ON c.cid=i.cid WHERE " + " AND ".join(clauses) + " ORDER BY i.time DESC,i.id DESC LIMIT ?"
        rows = self.db.execute(sql, (*params, limit + 1)).fetchall()
        items = [self.present(row, max_chars) for row in rows[:limit]]
        return {"items": items, "has_more": len(rows) > limit, "next_before": items[-1]["ref"] if len(rows) > limit else None,
                "freshness": self.freshness(conversation), "storage": "local", "search": "substring/FTS5"}

    def present(self, row, max_chars=1200):
        row = dict(row)
        content = row["content"]
        media = [{"id": m["id"], "kind": m["kind"], "downloaded": bool(m["asset_hash"])} for m in
                 self.db.execute("SELECT * FROM media WHERE message_ref=?", (row["id"],))]
        return {"ref": row["id"], "conversation": row.get("name") or self.store.resolve(row["cid"])["name"],
                "conversation_id": row["cid"], "message_id": row["mid"], "time": row["time"], "sender": row["sender"],
                "content": content[:max_chars], "truncated": len(content) > max_chars,
                "mention": row["mention_kind"], "media": media}

    def freshness(self, conversation=None):
        rows = self.store.source_rows()
        if conversation:
            cid = self.store.resolve(conversation)["cid"]
            rows = [r for r in rows if r["cid"] == cid]
        return [{"source": r["id"], "synced_until": r["watermark"], "mode": r["mode"], "expires_at": r["expires_at"]} for r in rows if r["mode"] != "ignored"]

    def context(self, ref, before=15, after=10, fresh=False):
        anchor = self.store.message(ref)
        refresh = None
        if fresh:
            when = cache.parse_time(anchor["time"])
            row = {"id": "context-" + str(ref), "name": self.store.resolve(anchor["cid"])["name"], "kind": "conversation",
                   "cid": anchor["cid"], "checkpoint_key": "context-window-" + str(ref)}
            with cache.locked(self.directory):
                refresh = self.sync_one(row, since=cache.timestamp(when-dt.timedelta(minutes=30)),
                                       until=min(now(), cache.timestamp(when+dt.timedelta(minutes=30))), max_pages=10)
        prev = self.db.execute("SELECT * FROM message_index WHERE profile=? AND cid=? AND (time<? OR(time=? AND id<?)) ORDER BY time DESC,id DESC LIMIT ?",
                              (self.profile, anchor["cid"], anchor["time"], anchor["time"], ref, before)).fetchall()
        following = self.db.execute("SELECT * FROM message_index WHERE profile=? AND cid=? AND (time>? OR(time=? AND id>?)) ORDER BY time,id LIMIT ?",
                              (self.profile, anchor["cid"], anchor["time"], anchor["time"], ref, after)).fetchall()
        return {"anchor": ref, "items": [self.present(r, 3000) for r in [*reversed(prev), anchor, *following]],
                "quote": json.loads(anchor["payload"]).get("quote"), "refresh": refresh,
                "note": "Local neighbors may be incomplete; --fresh fetches a bounded 30-minute window each side."}

    def inbox(self, limit=20):
        after = self.db.execute("SELECT seq FROM acknowledgements WHERE profile=?", (self.profile,)).fetchone()
        after = after[0] if after else 0
        changes = self.db.execute("SELECT * FROM changes WHERE profile=? AND seq>? ORDER BY seq LIMIT ?", (self.profile, after, limit)).fetchall()
        if not changes:
            return {"items": [], "batch": None}
        token = uuid.uuid4().hex
        through = changes[-1]["seq"]
        with self.db:
            self.db.execute("INSERT INTO batches(id,after_seq,through_seq,created_at) VALUES (?,?,?,?)", (token, after, through, now()))
            self.db.execute("DELETE FROM batches WHERE created_at<?", (cache.timestamp(dt.datetime.now(cache.TZ)-dt.timedelta(days=7)),))
        return {"batch": token, "through_seq": through, "items": [{"change_seq": r["seq"], "operation": r["operation"], **json.loads(r["payload"])} for r in changes]}

    def ack(self, token):
        row = self.db.execute("SELECT * FROM batches WHERE id=?", (token,)).fetchone()
        if not row:
            raise ValueError("Unknown/expired analysis batch")
        current = self.db.execute("SELECT seq FROM acknowledgements WHERE profile=?", (self.profile,)).fetchone()
        current = current[0] if current else 0
        if row["acknowledged"]:
            return {"acknowledged": token, "through_seq": row["through_seq"]}
        if current != row["after_seq"]:
            raise ValueError("Stale batch; query inbox again to avoid skipping unprocessed changes")
        with cache.locked(self.directory), self.db:
            cache.acknowledge(self.db, self.profile, row["through_seq"])
            self.db.execute("UPDATE batches SET acknowledged=1 WHERE id=?", (token,))
        return {"acknowledged": token, "through_seq": row["through_seq"]}

    def status(self):
        last = self.db.execute("SELECT result FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"profile": self.profile, "identity": self.store.get("identity"), "directory": str(self.directory),
                "messages": self.db.execute("SELECT COUNT(*) FROM message_index WHERE profile=?", (self.profile,)).fetchone()[0],
                "media": self.db.execute("SELECT COUNT(*) FROM media").fetchone()[0], "fts5_trigram": self.store.fts,
                "sources": [self.public_source(r) for r in self.store.source_rows()], "last_run": json.loads(last[0]) if last else None}
