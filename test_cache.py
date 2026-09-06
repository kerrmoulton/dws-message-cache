import copy
import tempfile
import unittest
from pathlib import Path

import cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.db = cache.database(self.directory / "test.sqlite3")
        self.config = {"profile": "fixture-corp:fixture-user", "sources": [], "overlap_seconds": 300}
        self.source = {"id": "test", "kind": "conversation", "conversation_id": "fixture-conversation"}
        self.start = "2026-09-04T16:00:00+08:00"
        self.end = "2026-09-04T18:00:00+08:00"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def page(self, ids, more=False, cursor=None, content="测试内容"):
        return {"success": True, "result": {
            "conversationMessagesList": [{"title": "测试群", "openConversationId": "fixture-conversation",
                "singleChat": False, "messages": [
                    {"openMessageId": mid, "createTime": "2026-09-04 17:00:00", "sender": "测试人", "content": content}
                    for mid in ids]}], "hasMore": more, "nextCursor": cursor}}

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def checkpoint(self):
        row = self.db.execute("SELECT until FROM checkpoints WHERE source_key=?", (cache.source_key(self.config, self.source),)).fetchone()
        return row[0] if row else None

    def run_sync(self, pages, **kwargs):
        stream = iter(pages)
        return cache.sync_source(self.db, self.config, self.source,
                                 since=kwargs.pop("since", self.start), until=kwargs.pop("until", self.end),
                                 runner=lambda *_: next(stream), **kwargs)

    def test_same_timestamp_pagination_and_repeated_sync(self):
        # Many messages at the identical second must paginate by cursor, not time.
        pages = [self.page(["a", "b"], True, "next"), self.page(["b", "c"])]
        first = self.run_sync(pages)
        second = self.run_sync(pages)
        self.assertEqual((first["inserted"], second["inserted"], self.count()), (3, 0, 3))
        self.assertEqual(self.checkpoint(), self.end)

    def test_failure_keeps_partial_data_but_not_checkpoint(self):
        with self.assertRaises(RuntimeError):
            self.run_sync([self.page(["a"], True, "next"), {"success": False, "errorCode": "DENIED"}])
        self.assertEqual(self.count(), 1)
        self.assertIsNone(self.checkpoint())
        recovered = self.run_sync([self.page(["a", "b"])])
        self.assertEqual(recovered["inserted"], 1)
        self.assertEqual(self.checkpoint(), self.end)

    def test_cursor_cycle_and_page_limit_do_not_advance(self):
        with self.assertRaisesRegex(RuntimeError, "cursor"):
            self.run_sync([self.page(["a"], True, "0")])
        self.assertIsNone(self.checkpoint())
        with self.assertRaisesRegex(RuntimeError, "Page limit"):
            self.run_sync([self.page(["a"], True, "next")], max_pages=1)
        self.assertIsNone(self.checkpoint())

    def test_incremental_overlap_and_empty_window(self):
        self.run_sync([self.page(["a"])])
        captured = []
        def runner(args, profile):
            captured.append(args)
            return {"success": True, "result": {"hasMore": False, "nextCursor": "ignored"}}
        cache.sync_source(self.db, self.config, self.source, until="2026-09-04T19:00:00+08:00", runner=runner)
        args = captured[0]
        self.assertEqual(args[args.index("--start") + 1], "2026-09-04T17:55:00+08:00")
        self.assertEqual(self.checkpoint(), "2026-09-04T19:00:00+08:00")

    def test_pending_ack_and_later_correction(self):
        self.run_sync([self.page(["a"])])
        first = cache.export_cache(self.db, self.config["profile"], self.directory)
        self.assertEqual(first["pending_messages"], 1)
        cache.acknowledge(self.db, self.config["profile"], first["through_seq"])
        self.assertEqual(cache.export_cache(self.db, self.config["profile"], self.directory)["pending_messages"], 0)
        self.run_sync([self.page(["a"], content="已修复，请验证")])
        update = cache.export_cache(self.db, self.config["profile"], self.directory)
        self.assertEqual(update["pending_messages"], 1)
        self.assertGreater(update["through_seq"], first["through_seq"])
        self.assertEqual(self.count(), 1)
        with self.assertRaises(ValueError):
            cache.acknowledge(self.db, self.config["profile"], 10000)

    def test_exclusion_and_cross_source_dedup(self):
        self.run_sync([self.page(["a"])])
        mention = {"id": "mentions", "kind": "mentions"}
        result = cache.sync_source(self.db, self.config, mention, self.start, self.end, runner=lambda *_: self.page(["a"]))
        self.assertEqual(result["inserted"], 0)
        self.config["exclude_conversation_ids"] = ["fixture-conversation"]
        result = self.run_sync([self.page(["b"])])
        self.assertEqual((result["filtered"], self.count()), (1, 1))

    def test_unknown_payload_does_not_look_empty(self):
        for response in ({"success": True, "result": {"items": [], "hasMore": False}},
                         {"success": True, "result": {}}, {"result": {"hasMore": "false"}}):
            with self.assertRaises(RuntimeError):
                self.run_sync([response])
        self.assertIsNone(self.checkpoint())

    def test_changed_target_and_profile_do_not_share_checkpoint(self):
        self.run_sync([self.page(["a"])])
        other = copy.deepcopy(self.config)
        other["profile"] = "another-corp:another-user"
        key = cache.source_key(other, self.source)
        self.assertIsNone(self.db.execute("SELECT until FROM checkpoints WHERE source_key=?", (key,)).fetchone())
        self.assertEqual(cache.export_cache(self.db, other["profile"], self.directory)["pending_messages"], 0)

    def test_no_signed_media_urls_in_cache(self):
        page = self.page(["a"])
        raw = page["result"]["conversationMessagesList"][0]["messages"][0]
        raw["resources"] = [{"url": "https://example.invalid/signed-secret"}]
        self.run_sync([page])
        self.assertNotIn("signed-secret", self.db.execute("SELECT payload FROM messages").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
