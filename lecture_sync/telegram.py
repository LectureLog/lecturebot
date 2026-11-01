from __future__ import annotations

import asyncio
import datetime as dt
import getpass
import json
import mimetypes
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, quote, urlsplit

import requests

from . import secrets
from .core import SyncError, cutoff, lecture_title, topic_of

CLOUD_LIMIT = 20 * 1024 * 1024
EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


class BotClient:
    def __init__(self, token, config):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise SyncError("Некорректный токен BotFather.")
        self.token, self.config = token, config
        self.base = config["telegram_api_url"]
        self.local = self.base != "https://api.telegram.org"
        self.bot_id = int(token.split(":", 1)[0])
        self.session = requests.Session()
        self.session.trust_env = False
        self.store = None

    def close(self):
        self.session.close()

    def call(self, method, **params):
        response = None
        try:
            response = self.session.post(f"{self.base}/bot{self.token}/{method}",
                                         json=params, timeout=(15, 60), allow_redirects=False)
            data = response.json()
            if response.status_code != 200 or not data.get("ok"):
                code = data.get("error_code", response.status_code)
                if code == 409:
                    raise SyncError("Bot API: конфликт getUpdates. Отключите webhook или другой получатель обновлений этого бота.")
                if code == 429:
                    raise SyncError("Bot API: слишком много запросов; повторите позднее.")
                raise SyncError(f"Bot API: {method} отклонён (HTTP/API {code}).")
            return data["result"]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise SyncError(f"Bot API: не удалось выполнить {method}; детали с токеном скрыты.") from None
        finally:
            if response is not None:
                response.close()

    async def get_messages(self, entity, ids):
        row = self.store.db.execute(
            "SELECT metadata FROM bot_files WHERE bot_id=? AND chat_id=? AND message_id=?",
            (self.bot_id, entity, ids)).fetchone()
        if not row:
            raise SyncError("Бот ещё не получил этот файл. Выполните scan после новой отправки записи в выбранный топик; Bot API не читает историю.")
        return cached_message(json.loads(row[0]))

    def fetch_file(self, message, target):
        info = self.call("getFile", file_id=message.file.file_id)
        value = info.get("file_path", "")
        target = Path(target)
        if self.local:
            root = self.config.get("telegram_local_files", "")
            if not root:
                raise SyncError("Для локального Bot API задайте telegram_local_files: корень его файлов на этом компьютере.")
            source = Path(value).resolve()
            if (not Path(value).is_absolute() or not source.is_relative_to(Path(root).resolve())
                    or not source.is_file() or source.stat().st_size != message.file.size):
                raise SyncError("Bot API вернул недоступный файл вне разрешённого каталога или неверного размера.")
            shutil.copyfile(source, target)
            return str(target)
        parts = PurePosixPath(value)
        if not value or parts.is_absolute() or ".." in parts.parts or "\\" in value:
            raise SyncError("Bot API вернул некорректный путь файла.")
        try:
            with self.session.get(f"{self.base}/file/bot{self.token}/{quote(value, safe='/')}",
                                  stream=True, timeout=(15, 120), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise SyncError("Bot API: не удалось скачать файл.")
                size = 0
                with target.open("wb") as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > message.file.size:
                            raise SyncError("Bot API передал больше данных, чем заявлено.")
                        stream.write(chunk)
        except requests.RequestException:
            raise SyncError("Загрузка из Bot API прервана; детали с токеном скрыты.") from None
        return str(target)

    async def download_media(self, message, file):
        return await asyncio.to_thread(self.fetch_file, message, file)


def cached_message(data):
    return NS(id=data["message_id"], date=dt.datetime.fromtimestamp(data["date"], dt.timezone.utc),
              reply_to=NS(forum_topic=True, reply_to_top_id=data["topic_id"], reply_to_msg_id=data["topic_id"]),
              file=NS(**data["file"]))


async def login(config):
    token = getpass.getpass("Токен Telegram-бота из BotFather: ").strip()
    client = BotClient(token, config)
    try:
        me = await asyncio.to_thread(client.call, "getMe")
        if not me.get("is_bot") or me.get("id") != client.bot_id:
            raise SyncError("Bot API не подтвердил аккаунт бота.")
        secrets.update(telegram_bot=dict(token=token))
        print(f"Бот @{me.get('username', '')}; токен сохранён в системной связке ключей.")
    finally:
        client.close()


@asynccontextmanager
async def connected(config):
    token = secrets.read().get("telegram_bot", {}).get("token")
    if not token:
        raise SyncError("Сначала выполните login-telegram и введите токен BotFather.")
    client = BotClient(token, config)
    try:
        yield client
    finally:
        client.close()


async def resolve_chat(client, config):
    if not config["chat_id"]:
        raise SyncError("Выполните configure --chat-id … --topic-ids … .")
    chat = await asyncio.to_thread(client.call, "getChat", chat_id=config["chat_id"])
    if chat.get("id") != config["chat_id"] or not chat.get("is_forum"):
        raise SyncError("Выбранный чат не является доступной боту группой с топиками.")
    return NS(id=chat["id"], entity=chat["id"])


def ingest(client, config, store, update):
    # Persist only group/topic identifiers and file metadata, never message text,
    # captions, user profiles or private conversations. Never trust forwarded origin.
    m = update.get("message")
    if not m:
        return
    chat = m.get("chat", {})
    if not chat.get("is_forum") or chat.get("type") != "supergroup":
        return
    db = store.db
    db.execute("INSERT OR REPLACE INTO bot_chats VALUES (?,?,?)",
               (client.bot_id, chat["id"], chat.get("title", "")))
    topic = m.get("message_thread_id", 1)
    title = (m.get("forum_topic_created") or m.get("forum_topic_edited") or {}).get("name", "")
    db.execute("INSERT INTO bot_topics VALUES (?,?,?,?) ON CONFLICT(bot_id,chat_id,topic_id) "
               "DO UPDATE SET name=CASE WHEN excluded.name!='' THEN excluded.name ELSE name END",
               (client.bot_id, chat["id"], topic, title))
    selected = (chat["id"] == config["chat_id"] if config["chat_id"] else chat.get("title") == config["chat_name"])
    if not selected or (config["topic_ids"] and topic not in config["topic_ids"]):
        return
    media = m.get("document") or m.get("video") or m.get("audio") or m.get("voice")
    if not media:
        return
    mime = media.get("mime_type", "")
    filename = media.get("file_name") or f"recording-{m['message_id']}{mimetypes.guess_extension(mime) or ''}"
    filename = Path(filename.replace("\\", "/")).name
    if Path(filename).suffix.lower() not in EXTENSIONS:
        return
    data = dict(message_id=m["message_id"], date=m["date"], topic_id=topic,
                file=dict(file_id=media["file_id"], name=filename,
                          size=media.get("file_size", 0), mime_type=mime))
    # First-seen immutable metadata prevents edited files from replacing an upload.
    db.execute("INSERT OR IGNORE INTO bot_files VALUES (?,?,?,?)",
               (client.bot_id, chat["id"], m["message_id"], json.dumps(data)))


def queue_cached(client, config, store):
    if not config["chat_id"] or not config["topic_ids"]:
        return
    for row in store.db.execute("SELECT metadata FROM bot_files WHERE bot_id=? AND chat_id=?",
                                (client.bot_id, config["chat_id"])).fetchall():
        message = cached_message(json.loads(row[0]))
        if message.date < cutoff(config):
            continue
        try:
            store.add(**recording_data(message, config["chat_id"], config))
        except SyncError:
            continue


async def scan(client, config, store, history=False):
    if history:
        raise SyncError("Bot API не читает историю. scan получает только ещё доступные обновления (Telegram хранит их до 24 часов).")
    client.store = store
    webhook = await asyncio.to_thread(client.call, "getWebhookInfo")
    if webhook.get("url"):
        raise SyncError("У бота установлен webhook. Используйте отдельного бота без webhook для LectureBot.")
    row = store.db.execute("SELECT next_id FROM bot_offsets WHERE bot_id=?", (client.bot_id,)).fetchone()
    offset = row[0] if row else 0
    # A bounded drain lets uploads/publication run even in busy groups.
    for _ in range(20):
        updates = await asyncio.to_thread(client.call, "getUpdates", offset=offset, limit=100,
                                         timeout=1, allowed_updates=["message"])
        if not updates:
            break
        next_id = max(u["update_id"] for u in updates) + 1
        try:
            with store.db:
                for update in updates:
                    ingest(client, config, store, update)
                store.db.execute("INSERT OR REPLACE INTO bot_offsets VALUES (?,?)", (client.bot_id, next_id))
        except Exception:
            # Next getUpdates acknowledges only the batch committed above.
            raise
        offset = next_id
        if len(updates) < 100:
            break
    queue_cached(client, config, store)


async def queue_one(value, config, store):
    message_id, hint = message_selection(value, config)
    existing = next((r for r in store.rows(chat_id=config["chat_id"]) if r["message_id"] == message_id), None)
    if existing:
        if existing["topic_id"] not in config["topic_ids"] or hint not in (None, existing["topic_id"]):
            raise SyncError("Запись не соответствует выбранному топику.")
        return message_id
    async with connected(config) as client:
        await scan(client, config, store)
        dialog = await resolve_chat(client, config)
        message = await client.get_messages(dialog.entity, ids=message_id)
        row = recording_data(message, dialog.id, config)
        if hint not in (None, row["topic_id"]):
            raise SyncError("Топик сообщения не совпадает с топиком ссылки.")
        store.add(**row)
    return message_id


def download_problem(client, row):
    if not getattr(client, "local", False) and row["size"] > CLOUD_LIMIT:
        return "Файл больше 20 МБ: облачный Bot API его не скачает. Нужен локальный Bot API сервер."
    return None


def message_selection(value: str, config):
    """Return a message ID and optional topic hint; never fetch a supplied URL."""
    if not config["chat_id"] or not config["topic_ids"]:
        raise SyncError("Сначала выполните configure.")
    value = value.strip()
    if value.isascii() and value.isdigit() and int(value) > 0:
        return int(value), None
    u = urlsplit(value)
    parts = u.path.strip("/").split("/")
    if (u.scheme != "https" or u.netloc not in ("t.me", "telegram.me")
            or len(parts) not in (3, 4) or parts[0] != "c"
            or not all(p.isascii() and p.isdigit() and int(p) > 0 for p in parts[1:])):
        raise SyncError("Укажите ID сообщения или ссылку https://t.me/c/CHAT_ID/[TOPIC_ID/]MESSAGE_ID.")
    if int("-100" + parts[1]) != config["chat_id"]:
        raise SyncError("Ссылка относится к другой группе; импорт отменён.")
    query = parse_qs(u.query, keep_blank_values=True)
    if "comment" in query:
        raise SyncError("Нужна прямая ссылка на сообщение с файлом, а не на комментарий.")
    hint = int(parts[2]) if len(parts) == 4 else None
    if "thread" in query:
        values = query["thread"]
        if len(values) != 1 or not values[0].isascii() or not values[0].isdigit() or int(values[0]) <= 0:
            raise SyncError("Некорректный ID топика в ссылке.")
        if hint is not None and hint != int(values[0]):
            raise SyncError("В ссылке указаны разные топики.")
        hint = int(values[0])
    if hint is not None and hint not in config["topic_ids"]:
        raise SyncError("Топик из ссылки не входит в выбранные предметы.")
    return int(parts[-1]), hint


def recording_data(message, chat_id, config):
    if not message or not getattr(message, "file", None):
        raise SyncError("В сообщении нет доступного аудио- или видеофайла.")
    subjects = dict(zip(config["topic_ids"], config["topic_names"], strict=True))
    topic = topic_of(message)
    if topic not in subjects:
        raise SyncError("Сообщение находится вне выбранных топиков.")
    file = message.file
    mime = file.mime_type or ""
    filename = file.name or f"recording-{message.id}{mimetypes.guess_extension(mime) or ''}"
    filename = Path(filename.replace("\\", "/")).name
    ext = Path(filename).suffix.lower()
    if ext not in EXTENSIONS:
        raise SyncError("Формат вложения не поддерживается для записи лекции.")
    if not file.size or file.size > config["max_file_bytes"]:
        raise SyncError("Размер записи превышает лимит или файл пуст.")
    if not mime.startswith(("video/", "audio/")):
        mime = mimetypes.guess_type(filename)[0] or ("video/mp4" if ext in {".mp4", ".mkv", ".mov", ".avi", ".webm"} else "audio/mpeg")
    return dict(chat_id=chat_id, message_id=message.id, topic_id=topic, subject=subjects[topic],
                filename=filename, size=file.size, mime=mime, published_at=message.date.isoformat(),
                title=lecture_title(subjects[topic], message.date.astimezone(cutoff(config).tzinfo), filename, chat_id, message.id))


def download_path(row, directory: Path):
    return directory / f"{abs(row['chat_id'])}-{row['message_id']}{Path(row['filename']).suffix.lower()}"


async def download(client, entity, row, directory: Path):
    path = download_path(row, directory)
    if path.exists() and path.stat().st_size == row["size"]:
        return path
    if shutil.disk_usage(directory).free < row["size"] + (100 << 20):
        raise SyncError("Недостаточно свободного места для записи.")
    message = await client.get_messages(entity, ids=row["message_id"])
    if not message or not message.file or message.file.size != row["size"]:
        raise SyncError("Исходное сообщение удалено или его файл изменился.")
    part = path.with_suffix(path.suffix + ".part")
    try:
        result = await client.download_media(message, file=str(part))
        if not result or not part.exists() or part.stat().st_size != row["size"]:
            raise SyncError("Файл скачан не полностью.")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path
