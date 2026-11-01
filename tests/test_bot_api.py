import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from lecture_sync import cli, telegram
from lecture_sync.core import DEFAULT, Store, SyncError, load_config, save_config
from test_sync import FakeServer, remote


def update(number=1, *, message_id=12, topic=10, size=5):
    return dict(update_id=number, message=dict(
        message_id=message_id, date=1793448000, message_thread_id=topic,
        chat=dict(id=-100123, title="Боталка 3 курс", type="supergroup", is_forum=True),
        caption="PRIVATE CAPTION", document=dict(file_id="file-12", file_name="lecture.mp4",
                                               mime_type="application/octet-stream", file_size=size)))


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db")
        self.config = DEFAULT | dict(chat_id=-100123, topic_ids=[10, 20])
        self.client = telegram.BotClient("123:TEST_TOKEN", self.config)
        self.client.store = self.store
        self.calls = []
        self.updates = []

        def call(method, **params):
            self.calls.append((method, params))
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                return [u for u in self.updates if u["update_id"] >= params["offset"]]
            if method == "getChat":
                return dict(id=-100123, is_forum=True)
            raise AssertionError(method)
        self.client.call = call

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.tmp.cleanup()

    async def test_filters_topics_date_private_messages_and_deduplicates(self):
        old = update(4, message_id=9)
        old["message"]["date"] = int(dt.datetime(2026, 10, 30, 20, tzinfo=dt.timezone.utc).timestamp())
        private = update(3)
        private["message"]["chat"] = dict(id=123, type="private")
        self.updates = [update(), update(2, topic=99), private, old]
        await telegram.scan(self.client, self.config, self.store)
        await telegram.scan(self.client, self.config, self.store)
        self.assertEqual([r["message_id"] for r in self.store.rows()], [12])
        self.assertEqual(self.store.rows()[0]["mime"], "video/mp4")
        self.assertEqual(self.store.db.execute("SELECT next_id FROM bot_offsets").fetchone()[0], 5)
        metadata = self.store.db.execute("SELECT metadata FROM bot_files").fetchone()[0]
        self.assertNotIn("PRIVATE CAPTION", metadata)
        self.assertEqual(self.calls[-1][1]["offset"], 5)

    async def test_failed_batch_is_not_acknowledged_and_rolls_back(self):
        self.updates = [update(), dict(update_id=2, message=dict(chat=dict(is_forum=True, type="supergroup")))]
        with self.assertRaises(KeyError):
            await telegram.scan(self.client, self.config, self.store)
        self.assertIsNone(self.store.db.execute("SELECT next_id FROM bot_offsets").fetchone())
        self.assertIsNone(self.store.db.execute("SELECT metadata FROM bot_files").fetchone())
        self.updates = [update()]
        await telegram.scan(self.client, self.config, self.store)
        self.assertEqual(self.store.rows()[0]["message_id"], 12)

    async def test_durable_inbox_survives_failure_before_queueing(self):
        self.updates = [update()]
        with patch.object(telegram, "queue_cached", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                await telegram.scan(self.client, self.config, self.store)
        self.assertFalse(self.store.rows())
        await telegram.scan(self.client, self.config, self.store)
        self.assertEqual(len(self.store.rows()), 1)

    async def test_cache_is_bot_scoped_and_cannot_fetch_arbitrary_history(self):
        self.updates = [update()]
        await telegram.scan(self.client, self.config, self.store)
        self.assertEqual((await self.client.get_messages(-100123, 12)).file.size, 5)
        self.client.bot_id = 999
        with self.assertRaisesRegex(SyncError, "не получил"):
            await self.client.get_messages(-100123, 12)

    async def test_webhook_and_history_fail_without_consuming_updates(self):
        with self.assertRaisesRegex(SyncError, "историю"):
            await telegram.scan(self.client, self.config, self.store, history=True)
        self.assertFalse(self.calls)
        self.client.call = lambda *args, **kwargs: {"url": "https://example.test"}
        with self.assertRaisesRegex(SyncError, "webhook"):
            await telegram.scan(self.client, self.config, self.store)

    async def test_cli_manual_import_uses_received_file_and_publishes(self):
        self.updates = [update()]
        self.config.update(base_url="https://lecture.test", upload_hosts=["files.test"])
        server = FakeServer()
        server.statuses[server.identity()] = "ready"

        def fetch(message, target):
            Path(target).write_bytes(b"hello")
            return target

        self.client.fetch_file = fetch
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def connected(*args):
            yield self.client

        args = cli.parser().parse_args(["import-one", "https://t.me/c/123/10/12"])
        with patch.object(telegram, "connected", connected), patch.object(cli, "remote", side_effect=lambda cfg: remote(server, cfg)):
            await cli.async_command(args, self.config, self.store, self.root)
        self.assertEqual(self.store.rows()[0]["publication_state"], "published")
        self.assertEqual(len(server.created), 1)

    async def test_large_file_stays_pending_without_presign_or_download(self):
        self.updates = [update(size=30 * 1024 * 1024)]
        await telegram.scan(self.client, self.config, self.store)
        self.config.update(base_url="https://lecture.test", upload_hosts=["files.test"])
        server = FakeServer()
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def connected(*args):
            yield self.client

        with patch.object(telegram, "connected", connected), patch.object(cli, "remote", side_effect=lambda cfg: remote(server, cfg)):
            with self.assertRaisesRegex(SyncError, "лимита"):
                await cli.import_pending(self.config, self.store, self.root)
        row = self.store.rows()[0]
        self.assertEqual(row["status"], "pending")
        self.assertIn("20 МБ", row["error"])
        self.assertFalse(any(c.url.endswith("/upload/presign") for c in server.calls))

    def test_local_server_file_is_copied_only_from_allowed_root(self):
        root = self.root / "server-files"
        root.mkdir()
        source = root / "recording.mp4"
        source.write_bytes(b"hello")
        message = telegram.cached_message(dict(update()["message"], topic_id=10,
            file=dict(file_id="x", size=5, name="a.mp4", mime_type="video/mp4")))
        self.client.local = True
        self.client.config = self.config | dict(telegram_local_files=str(root))
        self.client.call = lambda *args, **kwargs: dict(file_path=str(source))
        target = self.root / "copy.mp4"
        self.client.fetch_file(message, target)
        self.assertEqual(target.read_bytes(), b"hello")
        self.client.call = lambda *args, **kwargs: dict(file_path=str(self.root / "db"))
        with self.assertRaisesRegex(SyncError, "вне"):
            self.client.fetch_file(message, target)

    def test_api_error_never_echoes_token_or_remote_description(self):
        client = telegram.BotClient("123:SECRET", self.config)
        try:
            with patch.object(client.session, "post", side_effect=requests.ConnectionError("123:SECRET")):
                with self.assertRaises(SyncError) as exc:
                    client.call("getMe")
            self.assertNotIn("SECRET", str(exc.exception))
            self.assertFalse(client.session.trust_env)
        finally:
            client.close()

    def test_endpoint_rejects_external_servers_and_embedded_secrets(self):
        for value in ["http://evil.test", "https://evil.test", "http://user:secret@localhost:8081", "https://api.telegram.org/?secret=1"]:
            with self.subTest(value=value):
                path = self.root / "config.toml"
                save_config(path, self.config | dict(telegram_api_url=value))
                with self.assertRaises(SyncError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
