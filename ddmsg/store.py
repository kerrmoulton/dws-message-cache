import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3

import cache


def now():
    return cache.timestamp(dt.datetime.now(cache.TZ))


def mention_kind(message, identity, at_result=False):
    """Conservative: literal direct-name match AND API @me membership; no quoted/reaction matches."""
    text = message.get("content", "")
    if re.search(r"@(所有人|全体成员|全员|all|everyone)(?=$|[\s，。！？、:：,.;；!?\[\]])", text, re.I):
        return "all", "body contains an explicit all-members mention"
    # Verified API currently exposes only rendered mention text, not structured target IDs.
    if at_result:
        for name in identity.get("mention_names", []):
            if name and re.search(r"@" + re.escape(name) + r"(?=$|[\s，。！？、:：,.;；!?()（）\[\]])", text):
                return "direct", "DWS @me result plus exact personal mention in message body"
        return "unknown", "DWS @me result without a verifiable personal mention; no automatic promotion"
    return "none", "not verified through DWS @me results"


def media_refs(message):
    resources = []
    for r in message.get("resources", []):
        if r.get("resourceIdType") == "mediaId" and r.get("resourceId"):
            resources.append((r["resourceId"], r.get("resourceType", "file")))
    # Structured resources and rendered media IDs may encode the same image differently.
    # Prefer the complete structured resource list, otherwise parse the observed message notation.
    if not resources:
        kinds = {"图片": "image", "文件": "file", "视频": "video", "语音": "audio", "音频": "audio"}
        for match in re.finditer(r"\[(图片|文件|视频|语音|音频)消息\]\(mediaId=([^\)]+)\)", message.get("content", "")):
            resources.append((match.group(2), kinds[match.group(1)]))
    return list(dict.fromkeys(resources))


class Store:
    def __init__(self, directory, profile):
        self.directory, self.profile = Path(directory), profile
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = cache.database(self.directory / "messages.sqlite3")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations(cid TEXT PRIMARY KEY,name TEXT NOT NULL,single_chat INTEGER,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts(person_id TEXT PRIMARY KEY,name TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,kind TEXT NOT NULL,cid TEXT UNIQUE,name TEXT NOT NULL,
          mode TEXT NOT NULL CHECK(mode IN ('permanent','temporary','adhoc','disabled','ignored')),
          expires_at TEXT,reason TEXT,checkpoint_key TEXT NOT NULL,initial_since TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS message_index(id INTEGER PRIMARY KEY,profile TEXT NOT NULL,cid TEXT NOT NULL,
          mid TEXT NOT NULL,time TEXT NOT NULL,sender TEXT NOT NULL,sender_id TEXT,content TEXT NOT NULL,
          mention_kind TEXT NOT NULL DEFAULT 'none',mention_evidence TEXT,at_result INTEGER NOT NULL DEFAULT 0,
          UNIQUE(profile,cid,mid));
        CREATE INDEX IF NOT EXISTS idx_message_time ON message_index(profile,time DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_message_cid_time ON message_index(profile,cid,time DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_message_sender_time ON message_index(profile,sender,time DESC);
        CREATE INDEX IF NOT EXISTS idx_message_mention_time ON message_index(profile,mention_kind,time DESC);
        CREATE TABLE IF NOT EXISTS mention_actions(message_ref INTEGER PRIMARY KEY,processed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS context_jobs(cid TEXT PRIMARY KEY,since TEXT NOT NULL,until TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT);
        CREATE TABLE IF NOT EXISTS assets(hash TEXT PRIMARY KEY,path TEXT NOT NULL,size INTEGER NOT NULL,mime TEXT NOT NULL,
          analysis TEXT,analysis_model TEXT,analysis_at TEXT);
        CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY,message_ref INTEGER NOT NULL,resource_id TEXT NOT NULL,
          kind TEXT NOT NULL,asset_hash TEXT,last_error TEXT,UNIQUE(message_ref,resource_id));
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,after_seq INTEGER NOT NULL,through_seq INTEGER NOT NULL,
          created_at TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0);
        """)
        self.fts = False
        try:
            self.db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(content, tokenize='trigram');
            CREATE TRIGGER IF NOT EXISTS index_insert AFTER INSERT ON message_index BEGIN
              INSERT INTO message_fts(rowid,content) VALUES(new.id,new.content); END;
            CREATE TRIGGER IF NOT EXISTS index_update AFTER UPDATE OF content ON message_index WHEN old.content!=new.content BEGIN
              DELETE FROM message_fts WHERE rowid=old.id;
              INSERT INTO message_fts(rowid,content) VALUES(new.id,new.content); END;
            """)
            self.fts = True
        except sqlite3.OperationalError:
            pass  # Older SQLite: filtered substring search remains available.
        if self.fts and not self.get("fts_backfill_v1"):
            self.db.execute("INSERT OR REPLACE INTO message_fts(rowid,content) SELECT id,content FROM message_index")
            self.set("fts_backfill_v1", True)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, cache.dumps(value)))

    def migrate(self, config):
        if self.get("migration_v2"):
            return
        with self.db:
            for source in config.get("sources", []):
                cid = source.get("conversation_id")
                self.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (source["id"], source["kind"], cid, source.get("name", source["id"]),
                     "permanent" if source.get("enabled", True) else "disabled", None, "imported prototype configuration",
                     cache.source_key(config, source), None, now()))
                if cid:
                    self.conversation(cid, source.get("name", cid))
            for cid in config.get("exclude_conversation_ids", []):
                self.conversation(cid, cid)
                self.source(cid, "ignored", reason="imported exclusion")
            for row in self.db.execute("SELECT payload FROM messages WHERE profile=?", (self.profile,)).fetchall():
                self.index(json.loads(row[0]))
            self.set("initial_days", config.get("initial_days", 7))
            self.set("overlap_seconds", config.get("overlap_seconds", 300))
            self.set("temporary_hours", 72)
            self.set("max_auto_sources", 10)
            self.set("migration_v2", True)

    def conversation(self, cid, name, single=None):
        self.db.execute("""INSERT INTO conversations VALUES (?,?,?,?) ON CONFLICT(cid) DO UPDATE SET
          name=CASE WHEN excluded.name!=excluded.cid AND excluded.name!='' THEN excluded.name ELSE conversations.name END,
          single_chat=COALESCE(excluded.single_chat,conversations.single_chat),updated_at=excluded.updated_at""",
          (cid, name or cid, single, now()))

    def resolve(self, selector):
        row = self.db.execute("SELECT c.* FROM conversations c LEFT JOIN sources s ON c.cid=s.cid WHERE c.cid=? OR s.id=?", (selector, selector)).fetchone()
        if row:
            return dict(row)
        rows = self.db.execute("SELECT * FROM conversations WHERE name=?", (selector,)).fetchall()
        if not rows:
            rows = self.db.execute("SELECT * FROM conversations WHERE instr(lower(name),lower(?))>0 LIMIT 21", (selector,)).fetchall()
        if len(rows) != 1:
            raise ValueError(cache.dumps({"message": "Ambiguous or unknown conversation; use discover then an exact cid",
                                         "candidates": [dict(r) for r in rows]}))
        return dict(rows[0])

    def source(self, cid, mode, ttl=72, reason="user request", sid=None, initial_since=None):
        conversation = self.resolve(cid)
        cid = conversation["cid"]
        prior = self.db.execute("SELECT * FROM sources WHERE cid=?", (cid,)).fetchone()
        expiry = cache.timestamp(dt.datetime.now(cache.TZ) + dt.timedelta(hours=ttl)) if mode == "temporary" else None
        if prior:
            self.db.execute("UPDATE sources SET mode=?,expires_at=?,reason=?,updated_at=? WHERE cid=?",
                            (mode, expiry, reason, now(), cid))
            return prior["id"]
        sid = sid or "chat-" + hashlib.sha256(cid.encode()).hexdigest()[:12]
        key = hashlib.sha256((self.profile + ":" + cid).encode()).hexdigest()
        self.db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, "conversation", cid, conversation["name"], mode, expiry, reason, key, initial_since, now()))
        return sid

    def index(self, message, at_result=False):
        self.conversation(message["conversation_id"], message.get("conversation", ""), message.get("single_chat"))
        if message.get("sender_id"):
            self.db.execute("INSERT OR REPLACE INTO contacts VALUES (?,?,?)", (message["sender_id"], message.get("sender", ""), now()))
        kind, evidence = mention_kind(message, self.get("identity", {}), at_result)
        self.db.execute("""INSERT INTO message_index(profile,cid,mid,time,sender,sender_id,content,mention_kind,mention_evidence,at_result)
          VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile,cid,mid) DO UPDATE SET
          time=excluded.time,sender=excluded.sender,sender_id=excluded.sender_id,content=excluded.content,
          mention_kind=CASE WHEN excluded.at_result=1 OR excluded.mention_kind='all' THEN excluded.mention_kind ELSE message_index.mention_kind END,
          mention_evidence=CASE WHEN excluded.at_result=1 OR excluded.mention_kind='all' THEN excluded.mention_evidence ELSE message_index.mention_evidence END,
          at_result=MAX(message_index.at_result,excluded.at_result)""",
          (self.profile, message["conversation_id"], message["message_id"], message["time"], message.get("sender", ""),
           message.get("sender_id", ""), message.get("content", ""), kind, evidence, int(at_result)))
        ref = self.db.execute("SELECT id FROM message_index WHERE profile=? AND cid=? AND mid=?",
                             (self.profile, message["conversation_id"], message["message_id"])).fetchone()[0]
        for resource, media_kind in media_refs(message):
            self.db.execute("INSERT OR IGNORE INTO media(message_ref,resource_id,kind) VALUES (?,?,?)", (ref, resource, media_kind))
        return ref

    def source_rows(self):
        return [dict(r) for r in self.db.execute("SELECT s.*,c.until watermark FROM sources s LEFT JOIN checkpoints c ON c.source_key=s.checkpoint_key ORDER BY s.id")]

    def runtime_config(self):
        ignored = [r[0] for r in self.db.execute("SELECT cid FROM sources WHERE mode='ignored' AND cid IS NOT NULL")]
        return {"profile": self.profile, "initial_days": self.get("initial_days", 7),
                "overlap_seconds": self.get("overlap_seconds", 300), "page_size": 30, "exclude_conversation_ids": ignored}

    def automatic_mentions(self):
        rows = self.db.execute("""SELECT m.* FROM message_index m LEFT JOIN mention_actions a ON m.id=a.message_ref
          WHERE m.profile=? AND m.mention_kind='direct' AND a.message_ref IS NULL ORDER BY m.time DESC LIMIT 100""", (self.profile,)).fetchall()
        added = []
        for row in rows:
            cid = row["cid"]
            prior = self.db.execute("SELECT * FROM sources WHERE cid=?", (cid,)).fetchone()
            # A manual remove/ignore always wins over future automatic mentions.
            if prior and prior["mode"] in ("disabled", "ignored"):
                self.db.execute("INSERT INTO mention_actions VALUES (?,?)", (row["id"], now()))
                continue
            event_time = cache.parse_time(row["time"])
            expiry = event_time + dt.timedelta(hours=self.get("temporary_hours", 72))
            conv = self.resolve(cid)
            count = self.db.execute("SELECT COUNT(*) FROM sources WHERE mode='temporary' AND expires_at>?", (now(),)).fetchone()[0]
            if expiry > dt.datetime.now(cache.TZ) and conv["single_chat"] == 0 and (not prior or prior["mode"] in ("temporary", "adhoc")):
                if (prior and prior["mode"] == "temporary") or count < self.get("max_auto_sources", 10):
                    self.source(cid, "temporary", reason="direct mention " + row["mid"], initial_since=cache.timestamp(event_time - dt.timedelta(minutes=30)))
                    old_expiry = prior["expires_at"] if prior and prior["mode"] == "temporary" else None
                    self.db.execute("UPDATE sources SET expires_at=? WHERE cid=?", (max(old_expiry or "", cache.timestamp(expiry)), cid))
                    added.append(cid)
            start, end = cache.timestamp(event_time - dt.timedelta(minutes=30)), cache.timestamp(event_time + dt.timedelta(minutes=30))
            self.db.execute("""INSERT INTO context_jobs(cid,since,until) VALUES (?,?,?) ON CONFLICT(cid) DO UPDATE SET
              since=MIN(context_jobs.since,excluded.since),until=MAX(context_jobs.until,excluded.until)""", (cid, start, end))
            self.db.execute("INSERT INTO mention_actions VALUES (?,?)", (row["id"], now()))
        return added

    def message(self, ref):
        row = self.db.execute("SELECT i.*,m.payload FROM message_index i JOIN messages m ON m.profile=i.profile AND m.conversation_id=i.cid AND m.message_id=i.mid WHERE i.id=? AND i.profile=?", (ref, self.profile)).fetchone()
        if not row:
            raise ValueError("Unknown local message ref")
        return dict(row)
