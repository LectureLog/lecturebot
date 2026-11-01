import asyncio
import contextlib
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter

from lecture_sync import cli, secrets, telegram
from lecture_sync.core import DEFAULT, Store, SyncError, cutoff, lecture_title, load_config, save_config, topic_of
from lecture_sync.lecturelog import AmbiguousConfirm, AuthExpired, LectureLog, Page


def recording(**changes):
    return dict(chat_id=-100123, message_id=12, topic_id=10, subject="Сист прога",
                filename="lecture.mp4", size=5, mime="video/mp4", title="Lecture [tg100123_12]",
                published_at="2026-10-31T12:00:00+00:00") | changes


class FakeServer(BaseAdapter):
    def __init__(self):
        self.calls = []
        self.created = []
        self.lose_confirm_response = False
        self.login_redirect = False
        self.upload_host = "files.test"
        self.statuses = {}
        self.visibilities = {}
        self.publication_requests = []
        self.lose_publish_response = False
        self.bad_publish_response = False

    @staticmethod
    def identity(index=0):
        return f"00000000-0000-0000-0000-{index:012d}"

    def card(self, index, title, progress_only=False):
        from html import escape
        identity = self.identity(index)
        status = self.statuses.get(identity, "processing")
        visibility = self.visibilities.get(identity, "private")
        progress = f'<div id="lec-progress-{identity}"><div class="ll-status ll-status--{status}"></div></div>'
        if progress_only and status == "processing":
            return progress
        button = (f'<button class="ll-toggle ll-toggle--{visibility}" hx-post="/lectures/{identity}/visibility"></button>'
                  if status == "ready" or visibility == "public" else "")
        return (f'<article id="lec-{identity}"><input name="title" value="{escape(title, quote=True)}">'
                + progress + button + '</article>')

    def close(self):
        pass

    def send(self, request, **kwargs):
        self.calls.append(request)
        r = requests.Response()
        r.status_code = 200
        r.url = request.url
        r.request = request
        r._content = b""
        if self.login_redirect:
            r.status_code = 302
            r.headers["Location"] = "https://accounts.google.com/"
        elif request.url.endswith("/upload"):
            r._content = b'<section id="ll-upload" data-csrf="csrf-test"></section>'
        elif request.url.endswith("/lectures"):
            r._content = ('<section class="ll-lectures-section">' + ''.join(
                self.card(i, title)
                for i, title in enumerate(self.created)) + '</section>').encode()
        elif request.url.endswith("/status"):
            identity = request.url.split("/")[-2]
            index = next((i for i in range(len(self.created)) if self.identity(i) == identity), None)
            if index is None:
                r.status_code = 404
            else:
                r._content = self.card(index, self.created[index], progress_only=True).encode()
        elif request.url.endswith("/visibility"):
            from urllib.parse import parse_qs
            identity = request.url.split("/")[-2]
            self.publication_requests.append(request)
            assert parse_qs(request.body) == {"visibility": ["public"]}
            if self.statuses.get(identity) != "ready":
                r.status_code = 409
            else:
                index = next(i for i in range(len(self.created)) if self.identity(i) == identity)
                self.visibilities[identity] = "public"
                if self.lose_publish_response:
                    raise requests.Timeout("secret-that-must-not-be-logged")
                r._content = ("unknown" if self.bad_publish_response else self.card(index, self.created[index])).encode()
        elif request.url.endswith("/presign"):
            r._content = json.dumps(dict(put_url=f"https://{self.upload_host}/object?signature=SECRET", token="upload-secret", s3_key="object")).encode()
        elif request.method == "PUT":
            self.uploaded = request.body.read() if hasattr(request.body, "read") else request.body
        elif request.url.endswith("/confirm"):
            from urllib.parse import parse_qs
            self.created.append(parse_qs(request.body)["title"][0])
            if self.lose_confirm_response:
                raise requests.Timeout("sensitive-url-that-must-not-leak")
            r.headers["HX-Redirect"] = "/lectures"
        else:
            raise AssertionError(request.url)
        return r


def remote(server, config=None):
    config = DEFAULT | dict(base_url="https://lecture.test", upload_hosts=["files.test"],
                            chat_id=-100123, topic_ids=[10, 20]) | (config or {})
    a, b = requests.Session(), requests.Session()
    a.mount("https://", server)
    b.mount("https://", server)
    return LectureLog(config, "11111111-1111-1111-1111-111111111111", session=a, storage=b)


class CoreTests(unittest.TestCase):
    def test_topics_direct_nested_and_general(self):
        self.assertEqual(topic_of(NS(reply_to=None)), 1)
        self.assertEqual(topic_of(NS(reply_to=NS(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=10))), 10)
        self.assertEqual(topic_of(NS(reply_to=NS(forum_topic=True, reply_to_top_id=10, reply_to_msg_id=99))), 10)
        self.assertEqual(topic_of(NS(reply_to=NS(forum_topic=False, reply_to_msg_id=99))), 1)

    def test_date_timezone_and_title_identity(self):
        self.assertEqual(cutoff(DEFAULT).astimezone(dt.timezone.utc).isoformat(), "2026-10-30T21:00:00+00:00")
        title = lecture_title("Сист прога", cutoff(DEFAULT), "x" * 400 + ".mp4", -100123, 12)
        self.assertLessEqual(len(title), 200)
        self.assertTrue(title.endswith("[tg100123_12]"))

    def test_config_rejects_secrets_and_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            save_config(path, DEFAULT)
            self.assertEqual(load_config(path)["chat_name"], "Боталка 3 курс")
            for url in ("http://lecture.test", "https://user:password@lecture.test", "https://lecture.test/?token=secret"):
                save_config(path, DEFAULT | {"base_url": url})
                with self.assertRaises(SyncError):
                    load_config(path)

    def test_store_deduplication_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            store = Store(path)
            store.add(**recording())
            store.add(**recording())
            self.assertEqual(len(store.rows()), 1)
            store.advance(-100123, 10, 100)
            store.advance(-100123, 10, 90)
            self.assertEqual(store.cursor(-100123, 10), 100)
            store.mark(recording(), "confirming")
            store.close()
            store = Store(path)
            self.assertEqual(store.rows()[0]["status"], "uncertain")
            store.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_plaintext_keyring_is_rejected(self):
        with patch("keyring.get_keyring", return_value=NS()):
            with self.assertRaises(SyncError):
                secrets.backend()


class HTTPTests(unittest.TestCase):
    def test_csrf_and_cookies_do_not_reach_storage(self):
        server = FakeServer()
        client = remote(server)
        client.prepare()
        row = recording()
        upload = client.presign(row)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            path.write_bytes(b"hello")
            client.put(path, row, upload)
        client.confirm(row, upload)
        self.assertEqual(server.uploaded, b"hello")
        put = next(r for r in server.calls if r.method == "PUT")
        self.assertNotIn("Cookie", put.headers)
        self.assertNotIn("X-CSRF-Token", put.headers)
        self.assertNotIn("Authorization", put.headers)
        presign = next(r for r in server.calls if r.url.endswith("/presign"))
        self.assertIn("ll_session=", presign.headers["Cookie"])
        self.assertEqual(presign.headers["X-CSRF-Token"], "csrf-test")
        self.assertEqual(presign.headers["Origin"], "https://lecture.test")
        self.assertEqual(len(client.listings()[row["title"]]), 1)
        client.close()

    def test_storage_allowlist_fails_before_transfer(self):
        server = FakeServer()
        server.upload_host = "unexpected.test"
        client = remote(server)
        client.prepare()
        with self.assertRaises(SyncError) as exc:
            client.presign(recording())
        self.assertNotIn("SECRET", str(exc.exception))
        self.assertFalse(any(r.method == "PUT" for r in server.calls))
        client.close()

    def test_expired_auth_never_follows_google_redirect(self):
        server = FakeServer()
        server.login_redirect = True
        client = remote(server)
        with self.assertRaises(AuthExpired):
            client.prepare()
        self.assertEqual(len(server.calls), 1)
        client.close()

    def test_ambiguous_confirmation_is_not_retried(self):
        server = FakeServer()
        server.lose_confirm_response = True
        client = remote(server)
        client.prepare()
        with self.assertRaises(AmbiguousConfirm) as exc:
            client.confirm(recording(), dict(token="t", s3_key="s"))
        self.assertNotIn("sensitive", str(exc.exception))
        self.assertEqual(len(server.created), 1)
        client.close()

    def test_html_entities_and_duplicate_titles(self):
        p = Page('<article id="lec-a"><input name="title" value="A &amp; B"></article>'
                 '<article id="lec-b"><input name="title" value="A &amp; B"></article>')
        self.assertEqual(p.cards, {"A & B": ["a", "b"]})


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_and_reconcile_after_lost_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db")
            store.add(**recording())
            server = FakeServer()
            server.lose_confirm_response = True
            client = remote(server)
            message = NS(file=NS(size=5))

            class FakeTG:
                async def get_messages(self, entity, ids):
                    return message

                async def download_media(self, message, file):
                    Path(file).write_bytes(b"hello")
                    return file

            @contextlib.asynccontextmanager
            async def connected(*args):
                yield FakeTG()

            async def resolve(*args):
                return NS(id=-100123, entity=NS())

            with patch.object(cli, "remote", return_value=client), patch.object(telegram, "connected", connected), patch.object(telegram, "resolve_chat", resolve):
                with self.assertRaises(SyncError):
                    await cli.import_pending(client.config, store, root)
            self.assertEqual(store.rows()[0]["status"], "uncertain")
            # A successful server action followed by a lost response is recovered
            # using the source marker; a second confirm is never sent.
            self.assertEqual(cli.reconcile(store, client, client.config), 1)
            self.assertEqual(store.rows()[0]["status"], "submitted")
            self.assertEqual(len(server.created), 1)
            store.close()

    async def test_successful_import_removes_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db")
            store.add(**recording())
            server = FakeServer()
            client = remote(server)

            @contextlib.asynccontextmanager
            async def connected(*args):
                yield NS()

            async def resolve(*args):
                return NS(id=-100123, entity=NS())

            async def download(*args):
                path = root / "recording.mp4"
                path.write_bytes(b"hello")
                return path

            with patch.object(cli, "remote", return_value=client), patch.object(telegram, "connected", connected), patch.object(telegram, "resolve_chat", resolve), patch.object(telegram, "download", download):
                await cli.import_pending(client.config, store, root)
                await cli.import_pending(client.config, store, root)
            self.assertEqual(store.rows()[0]["status"], "submitted")
            self.assertFalse((root / "recording.mp4").exists())
            self.assertEqual(len(server.created), 1)
            store.close()



if __name__ == "__main__":
    unittest.main()
