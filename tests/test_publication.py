import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lecture_sync import cli
from lecture_sync.core import DEFAULT, Store, SyncError, load_config, save_config
from test_sync import FakeServer, recording, remote


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db")
        self.server = FakeServer()
        self.row = recording()
        self.server.created.append(self.row["title"])
        self.identity = self.server.identity()
        self.store.add(**self.row)
        self.store.mark(self.row, "submitted", self.identity)
        self.client = remote(self.server)
        self.client.prepare()

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.tmp.cleanup()

    async def publish(self):
        return await cli.publish_submitted(self.client.config, self.store, self.client)

    async def test_processing_waits_then_ready_publishes_once(self):
        self.assertEqual(await self.publish(), 0)
        self.assertEqual(self.store.rows()[0]["processing_state"], "processing")
        self.assertEqual(len(self.server.publication_requests), 0)
        self.server.statuses[self.identity] = "ready"
        self.assertEqual(await self.publish(), 1)
        self.assertEqual(await self.publish(), 0)
        row = self.store.rows()[0]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["processing_state"], "ready")
        self.assertEqual(row["publication_state"], "published")
        self.assertEqual(row["public_url"], f"https://lecture.test/read/{self.identity}")
        request = self.server.publication_requests[0]
        self.assertEqual(request.headers["X-CSRF-Token"], "csrf-test")
        self.assertEqual(request.headers["Origin"], "https://lecture.test")
        self.assertEqual(len(self.server.created), 1)
        self.assertEqual(len(self.server.publication_requests), 1)

    async def test_failed_is_not_published_or_automatically_retried(self):
        self.server.statuses[self.identity] = "failed"
        self.assertEqual(await self.publish(), 0)
        self.assertEqual(self.store.rows()[0]["processing_state"], "failed")
        self.assertFalse(self.server.publication_requests)
        self.assertFalse(any(r.url.endswith("/retry") for r in self.server.calls))
        # A later manual retry on the website can produce a ready lecture.
        self.server.statuses[self.identity] = "ready"
        self.assertEqual(await self.publish(), 1)

    async def test_lost_publish_response_recovers_without_second_post(self):
        self.server.statuses[self.identity] = "ready"
        self.server.lose_publish_response = True
        with self.assertRaises(SyncError):
            await self.publish()
        row = self.store.rows()[0]
        self.assertEqual(row["publication_state"], "pending")
        self.assertNotIn("secret-that", row["publication_error"])
        self.assertEqual(await self.publish(), 1)
        self.assertEqual(len(self.server.publication_requests), 1)
        self.assertEqual(self.store.rows()[0]["publication_state"], "published")

    async def test_existing_public_is_recorded_without_post(self):
        self.server.statuses[self.identity] = "ready"
        self.server.visibilities[self.identity] = "public"
        self.assertEqual(await self.publish(), 1)
        self.assertFalse(self.server.publication_requests)

    async def test_disable_prevents_status_and_publication_requests(self):
        self.client.config["publish_ready"] = False
        count = len(self.server.calls)
        self.assertEqual(await self.publish(), 0)
        self.assertEqual(len(self.server.calls), count)

    async def test_only_tracked_submitted_lectures_in_selected_topics(self):
        self.server.created.append("Unrelated manually uploaded lecture")
        self.server.statuses[self.server.identity(1)] = "ready"
        self.server.statuses[self.identity] = "ready"
        for change in ({"topic_id": 999}, {"chat_id": -100999},
                       {"published_at": "2026-10-30T10:00:00+00:00"}):
            with self.subTest(change=change):
                for key, value in change.items():
                    self.store.db.execute(f"UPDATE recordings SET {key}=?", (value,))
                self.store.db.commit()
                self.assertEqual(await self.publish(), 0)
                for key in change:
                    self.store.db.execute(f"UPDATE recordings SET {key}=?", (self.row[key],))
                self.store.db.commit()
        self.store.mark(self.row, "uncertain", self.identity)
        self.assertEqual(await self.publish(), 0)
        self.assertFalse(self.server.publication_requests)

    async def test_unknown_status_and_unconfirmed_publication_fail_closed(self):
        self.server.statuses[self.identity] = "unknown"
        with self.assertRaises(SyncError):
            await self.publish()
        self.assertFalse(self.server.publication_requests)
        self.server.statuses[self.identity] = "ready"
        self.server.bad_publish_response = True
        with self.assertRaises(SyncError):
            await self.publish()
        self.assertEqual(self.store.rows()[0]["publication_state"], "pending")

    async def test_known_publications_survive_a_restart(self):
        self.server.statuses[self.identity] = "ready"
        self.assertEqual(await self.publish(), 1)
        self.store.close()
        self.store = Store(Path(self.tmp.name) / "db")
        self.assertEqual(await self.publish(), 0)
        self.assertEqual(len(self.server.publication_requests), 1)

    async def test_sync_checks_publication_before_contacting_telegram(self):
        self.server.statuses[self.identity] = "ready"
        with patch.object(cli, "remote", return_value=self.client), patch.object(cli.telegram, "connected", side_effect=SyncError("Telegram недоступен")):
            with self.assertRaises(SyncError):
                await cli.async_command(type("Args", (), {"command": "sync"})(),
                                        self.client.config, self.store, Path(self.tmp.name))
        self.assertEqual(self.store.rows()[0]["publication_state"], "published")

    async def test_old_publication_failure_does_not_block_new_imports(self):
        @contextlib.asynccontextmanager
        async def connected(*args):
            yield object()
        with patch.object(cli, "publish_submitted", AsyncMock(side_effect=SyncError("Старая лекция удалена"))), \
             patch.object(cli.telegram, "connected", connected), \
             patch.object(cli.telegram, "scan", AsyncMock()), \
             patch.object(cli, "import_pending", AsyncMock()) as importer:
            await cli.async_command(type("Args", (), {"command": "sync"})(),
                                    self.client.config, self.store, Path(self.tmp.name))
            importer.assert_awaited_once()


class UpgradeTests(unittest.TestCase):
    def test_old_journal_and_settings_are_upgraded_without_losing_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            store = Store(path)
            store.add(**recording())
            store.mark(recording(), "submitted", FakeServer.identity())
            store.close()
            # Recreate the schema shipped in v0.2, including its existing row.
            db = sqlite3.connect(path)
            for column in ("processing_state", "publication_state", "publication_error", "public_url"):
                db.execute(f"ALTER TABLE recordings DROP COLUMN {column}")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.rows()[0]["status"], "submitted")
            self.assertEqual(store.rows()[0]["lecture_id"], FakeServer.identity())
            self.assertEqual(store.rows()[0]["publication_state"], "pending")
            store.close()
            config = Path(tmp) / "config.toml"
            old = DEFAULT.copy()
            old.pop("publish_ready")
            save_config(config, old)
            self.assertTrue(load_config(config)["publish_ready"])
            save_config(config, old | {"publish_ready": "false"})
            with self.assertRaises(SyncError):
                load_config(config)


if __name__ == "__main__":
    unittest.main()
