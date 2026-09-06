import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import cache
from ddmsg.app import App
from ddmsg import media, scheduler
from ddmsg.cli import main
from ddmsg.store import mention_kind, media_refs, now


class FakeClient:
    deadline = None
    binary = None

    def __init__(self):
        self.calls = []
        self.pages = []
        self.downloads = 0

    def __call__(self, args, profile=None):
        self.calls.append(args)
        if args[:2] == ["profile", "list"]:
            return {"profiles": [{"profile": "fixture:user", "userName": "测试我", "userId": "user"}]}
        if self.pages:
            response = self.pages.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"success": True, "result": {"hasMore": False}}

    def download(self, cid, mid, resource, path):
        self.downloads += 1
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture binary payload")


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"config.json"
        self.path.write_text(json.dumps({"profile": "fixture:user", "sources": [
            {"id": "group", "kind": "conversation", "conversation_id": "cid-fixture", "name": "绩效群"},
            {"id": "mentions", "kind": "mentions", "name": "@我"}]}))
        self.client = FakeClient()
        self.app = App(self.path, self.client)
        self.app.identity()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def message(self, mid="mid-1", cid="cid-fixture", content="指标复制需要修改", at=False, name="绩效群", when=None):
        value = {"conversation_id": cid, "message_id": mid, "conversation": name, "single_chat": False,
                 "time": when or now(), "sender": "示例同事", "sender_id": "fixture-sender", "content": content}
        with self.app.db:
            cache.save_message(self.app.db, self.app.profile, value)
            ref = self.app.store.index(value, at_result=at)
        return ref

    def test_local_query_and_chinese_search_never_use_network(self):
        ref = self.message()
        calls = len(self.client.calls)
        for term in ("指标", "指标复制", '复制" OR 1=1'):
            result = self.app.query(conversation="group", sender="示例同事", text=term)
            self.assertEqual(len(result["items"]), 0 if '"' in term else 1)
        self.assertEqual(result["storage"], "local")
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(self.app.context(ref)["items"][0]["ref"], ref)

    def test_equal_timestamp_query_pagination(self):
        when = now()
        for n in range(5):
            self.message(mid=str(n), when=when)
        first = self.app.query(limit=2)
        second = self.app.query(limit=2, before=first["next_before"])
        self.assertEqual(len({x["ref"] for x in first["items"]+second["items"]}), 4)

    def test_targeted_refresh_only_one_conversation(self):
        self.app.collect(["group"], contexts=False)
        remote = [c for c in self.client.calls if c[:3] == ["chat", "message", "search-advanced"]]
        self.assertEqual(len(remote), 1)
        self.assertEqual(remote[0][remote[0].index("--conversation-ids")+1], "cid-fixture")
        self.assertNotIn("--at-me", remote[0])

    def test_adhoc_does_not_join_default_collection(self):
        self.message(cid="cid-once", name="一次性群")
        self.app.collect(["cid-once"], contexts=False)
        mode = self.app.db.execute("SELECT mode FROM sources WHERE cid='cid-once'").fetchone()[0]
        self.assertEqual(mode, "adhoc")
        self.client.calls.clear()
        self.app.collect(contexts=False)
        self.assertFalse(any("cid-once" in c for c in self.client.calls))

    def test_direct_mentions_promote_temporarily_and_deduplicate(self):
        self.message(cid="cid-new", name="新工作群", content="@测试我 请看这个问题", at=True)
        with self.app.db:
            self.assertEqual(self.app.store.automatic_mentions(), ["cid-new"])
            self.assertEqual(self.app.store.automatic_mentions(), [])
        row = self.app.db.execute("SELECT * FROM sources WHERE cid='cid-new'").fetchone()
        self.assertEqual(row["mode"], "temporary")
        self.assertTrue(row["expires_at"])

    def test_at_all_and_unverifiable_mentions_never_promote(self):
        self.message(cid="cid-all", name="大群", content="@所有人 例行通知", at=True)
        self.message(mid="mid-other", cid="cid-unknown", name="未知群", content="这事帮忙看看", at=True)
        with self.app.db:
            self.assertEqual(self.app.store.automatic_mentions(), [])
        self.assertEqual(len(self.app.query(mention="direct")["items"]), 0)
        self.assertEqual(len(self.app.query(mention="all")["items"]), 1)
        self.assertEqual(len(self.app.query(mention="unknown")["items"]), 1)

    def test_mention_classifier_ignores_quote_and_reactions(self):
        identity = {"mention_names": ["测试我"]}
        self.assertEqual(mention_kind({"content": "已处理", "quote": {"content": "@测试我"}}, identity, True)[0], "unknown")
        self.assertEqual(mention_kind({"content": "@测试我同学 来看看"}, identity, True)[0], "unknown")
        self.assertEqual(mention_kind({"content": "@所有人 @测试我 来看看"}, identity, True)[0], "all")
        self.assertEqual(mention_kind({"content": "@测试我 看看"}, identity, False)[0], "none")

    def test_remove_and_ignore_override_future_mentions(self):
        for mode in ("disabled", "ignored"):
            self.app.add_source("group", mode)
            self.message(mid=mode, content="@测试我 请跟进", at=True)
            with self.app.db:
                self.assertEqual(self.app.store.automatic_mentions(), [])
            self.assertEqual(self.app.db.execute("SELECT mode FROM sources WHERE id='group'").fetchone()[0], mode)

    def test_temporary_expiry_and_old_mentions(self):
        self.app.add_source("group", "temporary")
        with self.app.db:
            self.app.db.execute("UPDATE sources SET expires_at='2000-01-01T00:00:00+08:00' WHERE id='group'")
        self.app.collect(contexts=False)
        self.assertFalse(any("cid-fixture" in call for call in self.client.calls))
        self.message(cid="old", name="旧群", content="@测试我 旧消息", at=True, when="2020-01-01T00:00:00+08:00")
        with self.app.db:
            self.app.store.automatic_mentions()
        self.assertIsNone(self.app.db.execute("SELECT * FROM sources WHERE cid='old'").fetchone())

    def test_config_mutation_does_not_reset_watermark(self):
        self.app.collect(["group"], contexts=False)
        old = self.app.store.source_rows()
        old = next(r["watermark"] for r in old if r["id"] == "group")
        self.app.add_source("group", "temporary")
        self.assertEqual(next(r["watermark"] for r in self.app.store.source_rows() if r["id"]=="group"), old)

    def test_context_fresh_does_not_advance_source_watermark(self):
        ref = self.message(when="2026-09-04T17:00:00+08:00")
        self.app.context(ref, fresh=True)
        self.assertIsNone(next(r["watermark"] for r in self.app.store.source_rows() if r["id"]=="group"))

    def test_ambiguous_name_cannot_mutate_sources(self):
        self.message(cid="cid-second", name="绩效群")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.app.add_source("绩效群", "permanent")

    def test_media_download_cache_and_analysis_reuse(self):
        ref = self.message(content="[图片消息](mediaId=fixture-image)")
        mid = media.list_media(self.app, ref)["items"][0]["id"]
        first, second = media.download(self.app, mid), media.download(self.app, mid)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(self.client.downloads, 1)
        analysis = Path(self.temp.name)/"analysis.txt"
        analysis.write_text("保存时提示请选择组织", encoding="utf-8")
        media.annotate(self.app, mid, analysis, "fixture-model")
        self.assertEqual(media.list_media(self.app, ref)["items"][0]["analysis"], "保存时提示请选择组织")

    def test_media_refs_prefer_structured_resources(self):
        refs = media_refs({"content": "[图片消息](mediaId=encoded)", "resources": [{"resourceId": "canonical", "resourceIdType": "mediaId", "resourceType": "image"}]})
        self.assertEqual(refs, [("canonical", "image")])

    def test_inbox_ack_does_not_skip_new_arrivals(self):
        for n in range(3):
            self.message(mid=str(n))
        first = self.app.inbox(2)
        self.message(mid="late")
        self.app.ack(first["batch"])
        second = self.app.inbox(10)
        self.assertEqual(len(second["items"]), 2)
        self.app.ack(first["batch"])  # idempotent

    def test_errors_are_not_reported_as_fresh(self):
        self.client.pages = [RuntimeError("permission denied")]
        result = self.app.collect(["group"], contexts=False)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIsNone(next(r["watermark"] for r in self.app.store.source_rows() if r["id"] == "group"))

    def test_migration_retains_old_messages_and_checkpoint(self):
        self.message()
        self.app.collect(["group"], contexts=False)
        before = self.app.status()["messages"]
        another = App(self.path, self.client)
        try:
            self.assertEqual(another.status()["messages"], before)
            self.assertIsNotNone(next(r["watermark"] for r in another.store.source_rows() if r["id"] == "group"))
        finally:
            another.close()

    def test_cli_invalid_bound_and_json_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--config", str(self.path), "query", "--fresh"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_person_discovery_resolves_dws_identity_without_following(self):
        self.client.pages = [{"result": [{"author": "同事", "openDingTalkId": "verified-person"}]},
                             {"result": {"conversationInfo": {"openConversationId": "new-dm", "title": "同事", "singleChat": True}}}]
        result = self.app.discover("同事", "person", fresh=True)
        self.assertEqual(result["items"][0]["cid"], "new-dm")
        self.assertIsNone(self.app.db.execute("SELECT * FROM sources WHERE cid='new-dm'").fetchone())

    def test_disable_mentions_feed_and_restore(self):
        self.app.add_source("mentions", "disabled")
        self.client.calls.clear()
        self.app.collect(contexts=False)
        self.assertFalse(any("--at-me" in args for args in self.client.calls))
        self.app.add_source("mentions", "permanent")
        self.app.collect(contexts=False)
        self.assertTrue(any("--at-me" in args for args in self.client.calls))

    def test_zero_auto_limit_does_not_promote_adhoc(self):
        self.message(cid="new", name="新群", content="@测试我 请看", at=True)
        self.app.add_source("new", "adhoc")
        with self.app.db:
            self.app.store.set("max_auto_sources", 0)
            self.app.store.automatic_mentions()
        self.assertEqual(self.app.db.execute("SELECT mode FROM sources WHERE cid='new'").fetchone()[0], "adhoc")
        self.assertEqual(self.app.db.execute("SELECT COUNT(*) FROM context_jobs WHERE cid='new'").fetchone()[0], 1)


class SchedulerTests(unittest.TestCase):
    def test_launchd_hybrid_vs_interval(self):
        hybrid = scheduler.launchd_definition("/tmp/space name/config.json", "hybrid", 600, "/tmp/dws")
        interval = scheduler.launchd_definition("/tmp/space name/config.json", "interval", 600, "/tmp/dws")
        self.assertTrue(hybrid["KeepAlive"])
        self.assertNotIn("StartInterval", hybrid)
        self.assertEqual(interval["StartInterval"], 600)
        self.assertNotIn("KeepAlive", interval)
        parsed = plistlib.loads(plistlib.dumps(hybrid))
        self.assertIn(str(Path("/tmp/space name/config.json").resolve()), parsed["ProgramArguments"])

    def test_windows_xml_with_unicode_spaces_and_modes(self):
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        for mode in ("hybrid", "interval"):
            doc = scheduler.windows_definition("/tmp/工作 目录/config.json", mode, 600, python=r"C:\Program Files\Python\python.exe", user=r"PC\Alice")
            root = ET.fromstring(doc)
            self.assertEqual(root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns), "IgnoreNew")
            self.assertEqual(root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns), "InteractiveToken")
            self.assertEqual(root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), r"C:\Program Files\Python\python.exe")
            arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
            self.assertIn('"' + str(Path("/tmp/工作 目录/config.json").resolve()) + '"', arguments)
            self.assertEqual(root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns), "PT0S" if mode == "hybrid" else "PT240S")


if __name__ == "__main__":
    unittest.main()
