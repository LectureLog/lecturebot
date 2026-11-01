from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import sqlite3
import stat
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class SyncError(Exception):
    """An error whose message is safe to show without leaking credentials."""


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise SyncError("Каталог состояния должен принадлежать текущему пользователю.")
    path.chmod(0o700)
    return path


def state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return private_dir(root / "lecture-sync")


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "lecture-sync/config.toml"


DEFAULT = dict(
    base_url="https://lecturelog.sarvizza.com", chat_name="Боталка 3 курс", chat_id=0,
    topic_names=["Сист прога", "Формалки"], topic_ids=[], since="2026-10-31",
    timezone="Europe/Moscow", extract_slides=True, max_file_bytes=5 << 30,
    upload_hosts=[], publish_ready=True,
    telegram_api_url="https://api.telegram.org", telegram_local_files="",
)


def save_config(path: Path, config: dict) -> None:
    private_dir(path.parent)
    text = "# Non-secret configuration; secrets live in the OS keyring.\n"
    for key, value in config.items():
        text += f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SyncError("Сначала выполните lecturebot init.")
    config = DEFAULT | tomllib.loads(path.read_text(encoding="utf-8"))
    u = urlsplit(config["base_url"])
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
        raise SyncError("base_url должен быть HTTPS-адресом сайта без пути и секретов.")
    config["base_url"] = config["base_url"].rstrip("/")
    cutoff(config)  # Validate date and timezone before accessing either account.
    if not isinstance(config["max_file_bytes"], int) or config["max_file_bytes"] <= 0:
        raise SyncError("max_file_bytes должен быть положительным целым числом.")
    if not isinstance(config["publish_ready"], bool):
        raise SyncError("publish_ready должен быть true или false.")
    endpoint = urlsplit(config["telegram_api_url"])
    if (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path not in ("", "/") or not (
                config["telegram_api_url"].rstrip("/") == "https://api.telegram.org"
                or (endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "localhost", "::1")))):
        raise SyncError("telegram_api_url: допустим облачный API или локальный HTTP-сервер на loopback.")
    config["telegram_api_url"] = config["telegram_api_url"].rstrip("/")
    if (not isinstance(config["chat_id"], int) or isinstance(config["chat_id"], bool)
            or not isinstance(config["topic_ids"], list)
            or any(type(t) is not int or t <= 0 for t in config["topic_ids"])
            or len(set(config["topic_ids"])) != len(config["topic_ids"])
            or (config["topic_ids"] and len(config["topic_ids"]) != len(config["topic_names"]))):
        raise SyncError("Задайте chat_id и уникальные положительные topic_ids в порядке topic_names.")
    return config


def cutoff(config: dict) -> dt.datetime:
    return dt.datetime.combine(dt.date.fromisoformat(config["since"]), dt.time(), ZoneInfo(config["timezone"]))


def topic_of(message) -> int:
    reply = getattr(message, "reply_to", None)
    if reply and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or reply.reply_to_msg_id
    return 1  # General topic has no thread header.


def source_marker(chat_id: int, message_id: int) -> str:
    return f"[tg{abs(chat_id)}_{message_id}]"


def lecture_title(subject: str, date: dt.datetime, filename: str, chat_id: int, message_id: int) -> str:
    marker = source_marker(chat_id, message_id)
    stem = Path(filename.replace("\\", "/")).stem
    prefix = f"{subject} — {date:%Y-%m-%d} — {stem}"
    return prefix[:199 - len(marker)].rstrip() + " " + marker


@contextlib.contextmanager
def run_lock(directory: Path):
    with (directory / "run.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Другой экземпляр инструмента уже работает.") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS recordings (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL, subject TEXT NOT NULL, filename TEXT NOT NULL,
                size INTEGER NOT NULL, mime TEXT NOT NULL, title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', lecture_id TEXT,
                error TEXT, PRIMARY KEY(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS cursors (
                chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
                PRIMARY KEY(chat_id, topic_id)
            );
            CREATE TABLE IF NOT EXISTS bot_offsets (
                bot_id INTEGER PRIMARY KEY, next_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_chats (
                bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(bot_id, chat_id)
            );
            CREATE TABLE IF NOT EXISTS bot_topics (
                bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                name TEXT NOT NULL, PRIMARY KEY(bot_id, chat_id, topic_id)
            );
            CREATE TABLE IF NOT EXISTS bot_files (
                bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                metadata TEXT NOT NULL, PRIMARY KEY(bot_id, chat_id, message_id)
            );
        """)
        # Upgrade existing import journals without resetting deduplication.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(recordings)")}
        for name, definition in {
            "processing_state": "TEXT",
            "publication_state": "TEXT NOT NULL DEFAULT 'pending'",
            "publication_error": "TEXT",
            "public_url": "TEXT",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE recordings ADD COLUMN {name} {definition}")
        # A crash after sending confirm cannot safely be retried automatically.
        self.db.execute("UPDATE recordings SET status='uncertain' WHERE status='confirming'")
        self.db.commit()

    def add(self, **row):
        keys = ",".join(row)
        slots = ",".join("?" for _ in row)
        self.db.execute(f"INSERT OR IGNORE INTO recordings ({keys}) VALUES ({slots})", tuple(row.values()))
        self.db.commit()

    def rows(self, status=None, chat_id=None):
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if chat_id is not None:
            where.append("chat_id=?")
            args.append(chat_id)
        return list(self.db.execute("SELECT * FROM recordings" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY chat_id,message_id", args))

    def mark(self, row, status, lecture_id=None, error=None):
        self.db.execute("UPDATE recordings SET status=?, lecture_id=?, error=? WHERE chat_id=? AND message_id=?",
                        (status, lecture_id, error, row["chat_id"], row["message_id"]))
        self.db.commit()

    def cursor(self, chat_id, topic_id):
        row = self.db.execute("SELECT last_id FROM cursors WHERE chat_id=? AND topic_id=?", (chat_id, topic_id)).fetchone()
        return row[0] if row else 0

    def publication(self, row, processing_state, *, published=False, public_url=None, error=None):
        self.db.execute("""UPDATE recordings SET processing_state=?, publication_state=?,
                        public_url=?, publication_error=? WHERE chat_id=? AND message_id=?""",
                        (processing_state, "published" if published else "pending", public_url,
                         error, row["chat_id"], row["message_id"]))
        self.db.commit()

    def advance(self, chat_id, topic_id, message_id):
        self.db.execute("INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(chat_id,topic_id) DO UPDATE SET last_id=MAX(last_id,excluded.last_id)", (chat_id, topic_id, message_id))
        self.db.commit()

    def close(self):
        self.db.close()
