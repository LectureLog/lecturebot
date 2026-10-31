import contextlib
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from lecture_sync import cli, telegram
from lecture_sync.core import DEFAULT, Store, SyncError
from test_sync import FakeServer, recording, remote


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = DEFAULT | dict(chat_id=-100123, topic_ids=[10, 20])

    def test_private_links_topic_links_and_numeric_ids(self):
        for value, expected in [
            ("12", (12, None)),
            ("https://t.me/c/123/12", (12, None)),
            ("https://t.me/c/123/10/12?single", (12, 10)),
            ("https://telegram.me/c/123/12?thread=10", (12, 10)),
        ]:
            with self.subTest(value=value):
                self.assertEqual(telegram.message_selection(value, self.config), expected)

    def test_wrong_chat_topic_comments_and_arbitrary_urls_are_rejected(self):
        for value in ["https://t.me/c/999/12", "https://t.me/c/123/99/12",
                      "https://t.me/c/123/12?comment=55", "https://evil.test/c/123/12",
                      "https://t.me/c/123/10/12?thread=20", "0", "-12",
                      "https://t.me/c/123/12?thread=bad"]:
            with self.subTest(value=value), self.assertRaises(SyncError):
                telegram.message_selection(value, self.config)


class ImportOneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.config = DEFAULT | dict(chat_id=-100123, topic_ids=[10, 20],
                                    base_url="https://lecture.test", upload_hosts=["files.test"])
        self.server = FakeServer()
        self.args = NS(message="https://t.me/c/123/10/12", timeout=60, poll_interval=30)
        self.message = NS(id=12, date=dt.datetime(2026, 10, 31, 12, tzinfo=dt.timezone.utc),
                          reply_to=NS(forum_topic=True, reply_to_top_id=10, reply_to_msg_id=10),
                          file=NS(name="lecture.mp4", size=5, mime_type="video/mp4"))
        self.downloads = 0
        test = self

        class TG:
            async def get_messages(self, entity, ids):
                return test.message

            async def download_media(self, message, file):
                test.downloads += 1
                Path(file).write_bytes(b"hello")
                return file

        @contextlib.asynccontextmanager
        async def connected():
            yield TG()

        async def resolve(*args):
            return NS(id=-100123, entity=NS())

        self.patches = [
            patch.object(telegram, "connected", connected),
            patch.object(telegram, "resolve_chat", resolve),
            patch.object(cli, "remote", side_effect=lambda cfg: remote(self.server, cfg)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    async def run_one(self):
        return await cli.async_command(NS(command="import-one", **vars(self.args)),
                                       self.config, self.store, self.root)

    async def test_one_message_goes_through_upload_wait_and_publication(self):
        # Another ready lecture and another pending recording must stay untouched.
        self.server.created.append("Unrelated manually uploaded lecture")
        self.server.statuses[self.server.identity(0)] = "ready"
        other = recording(message_id=13, title="Another pending [tg100123_13]")
        self.store.add(**other)

        async def finish_processing(seconds):
            self.assertLessEqual(seconds, 30)
            self.server.statuses[self.server.identity(1)] = "ready"

        with patch.object(cli.asyncio, "sleep", side_effect=finish_processing):
            await self.run_one()
        selected = next(r for r in self.store.rows() if r["message_id"] == 12)
        other = next(r for r in self.store.rows() if r["message_id"] == 13)
        self.assertEqual(selected["publication_state"], "published")
        self.assertEqual(other["status"], "pending")
        self.assertEqual(self.downloads, 1)
        self.assertEqual(len(self.server.created), 2)
        self.assertEqual(len(self.server.publication_requests), 1)
        self.assertIn(self.server.identity(1), self.server.publication_requests[0].url)
        self.assertEqual(self.store.cursor(-100123, 10), 0)
        self.assertFalse(list((self.root / "downloads").glob("*.mp4")))

    async def test_explicit_manual_import_works_before_since_and_with_auto_publication_off(self):
        self.message.date = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        self.config["publish_ready"] = False
        self.server.statuses[self.server.identity()] = "ready"
        await self.run_one()
        self.assertEqual(self.store.rows()[0]["publication_state"], "published")

    async def test_timeout_can_be_resumed_without_uploading_again(self):
        self.args.timeout = 0.001
        self.args.poll_interval = 0.001
        with self.assertRaisesRegex(SyncError, "Время ожидания"):
            await self.run_one()
        self.assertEqual(self.store.rows()[0]["status"], "submitted")
        self.assertFalse(self.server.publication_requests)
        self.server.statuses[self.server.identity()] = "ready"
        self.args.timeout = 60
        await self.run_one()
        await self.run_one()
        self.assertEqual(len(self.server.created), 1)
        self.assertEqual(self.downloads, 1)
        self.assertEqual(len(self.server.publication_requests), 1)

    async def test_failed_processing_exits_without_publication_or_retry(self):
        self.server.statuses[self.server.identity()] = "failed"
        with self.assertRaisesRegex(SyncError, "завершилась ошибкой"):
            await self.run_one()
        self.assertEqual(self.store.rows()[0]["status"], "submitted")
        self.assertFalse(self.server.publication_requests)
        self.assertFalse(any(r.url.endswith("/retry") for r in self.server.calls))

    async def test_ambiguous_upload_is_not_sent_twice(self):
        self.server.lose_confirm_response = True
        with self.assertRaises(SyncError):
            await self.run_one()
        self.assertEqual(self.store.rows()[0]["status"], "uncertain")
        self.server.statuses[self.server.identity()] = "ready"
        await self.run_one()
        self.assertEqual(len(self.server.created), 1)
        self.assertEqual(self.downloads, 1)

    async def test_missing_file_and_out_of_scope_topic_do_not_upload(self):
        for change in [dict(file=None), dict(reply_to=NS(forum_topic=True, reply_to_top_id=99, reply_to_msg_id=99))]:
            original = vars(self.message).copy()
            vars(self.message).update(change)
            with self.assertRaises(SyncError):
                await self.run_one()
            vars(self.message).clear()
            vars(self.message).update(original)
        self.assertFalse(self.store.rows())
        self.assertFalse(self.server.calls)

    async def test_ambiguous_record_without_remote_match_is_not_reuploaded(self):
        self.store.add(**recording())
        self.store.mark(recording(), "uncertain")
        with self.assertRaisesRegex(SyncError, "не подтверждена"):
            await self.run_one()
        self.assertFalse(self.server.created)
        self.assertEqual(self.downloads, 0)

    async def test_invalid_timeout_does_not_connect(self):
        self.args.timeout = 0
        with self.assertRaisesRegex(SyncError, "положительными"):
            await self.run_one()
        self.assertFalse(self.store.rows())
        self.assertFalse(self.server.calls)


if __name__ == "__main__":
    unittest.main()
