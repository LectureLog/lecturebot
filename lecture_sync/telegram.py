from __future__ import annotations

import getpass
import inspect
import mimetypes
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import secrets
from .core import SyncError, cutoff, lecture_title, topic_of

EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


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


async def queue_one(value, config, store):
    message_id, hint = message_selection(value, config)
    existing = next((r for r in store.rows(chat_id=config["chat_id"]) if r["message_id"] == message_id), None)
    if existing:
        if existing["topic_id"] not in config["topic_ids"] or hint not in (None, existing["topic_id"]):
            raise SyncError("Запись не соответствует выбранному топику.")
        return message_id
    async with connected() as client:
        dialog = await resolve_chat(client, config)
        message = await client.get_messages(dialog.entity, ids=message_id)
        row = recording_data(message, dialog.id, config)
        if hint not in (None, row["topic_id"]):
            raise SyncError("Топик сообщения не совпадает с топиком ссылки.")
        # Manual selection is explicit and may precede the automatic cutoff.
        # Do not advance cursors: this must not skip any other messages.
        store.add(**row)
    return message_id


async def login():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(input("Telegram api_id (my.telegram.org/apps): ").strip())
    api_hash = getpass.getpass("Telegram api_hash: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash, receive_updates=False)
    try:
        await client.start(phone=lambda: input("Номер вашего основного Telegram-аккаунта: "),
                           code_callback=lambda: getpass.getpass("Код входа Telegram: "),
                           password=lambda: getpass.getpass("Пароль 2FA Telegram: "))
        secrets.update(telegram=dict(api_id=api_id, api_hash=api_hash, session=client.session.save()))
    finally:
        await client.disconnect()


@asynccontextmanager
async def connected():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    data = secrets.read().get("telegram")
    if not data:
        raise SyncError("Сначала выполните login-telegram.")
    client = TelegramClient(StringSession(data["session"]), data["api_id"], data["api_hash"], receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SyncError("Telegram-сессия отозвана. Повторите login-telegram.")
        yield client
    finally:
        await client.disconnect()


async def resolve_chat(client, config):
    # Enumerating dialogs populates access_hash without storing other chats on disk.
    matches = []
    async for dialog in client.iter_dialogs():
        if (config["chat_id"] and dialog.id == config["chat_id"]) or (not config["chat_id"] and dialog.name == config["chat_name"]):
            matches.append(dialog)
    if len(matches) != 1:
        raise SyncError("Не удалось однозначно выбрать группу. Выполните dialogs и задайте chat_id.")
    if not getattr(matches[0].entity, "forum", False):
        raise SyncError("Выбранный чат не является группой с топиками.")
    return matches[0]


async def topics(client, entity):
    from telethon import functions
    request = getattr(functions.messages, "GetForumTopicsRequest", None) or getattr(functions.channels, "GetForumTopicsRequest", None)
    if request is None:
        raise SyncError("Эта версия Telethon не поддерживает получение топиков.")
    field = "peer" if "peer" in inspect.signature(request).parameters else "channel"
    offset_date, offset_id, offset_topic = None, 0, 0
    result, seen = [], set()
    while True:
        page = await client(request(**{field: entity}, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100))
        new = [t for t in page.topics if hasattr(t, "title") and t.id not in seen]
        if not new:
            break
        result.extend(new)
        seen.update(t.id for t in new)
        last = new[-1]
        offset_topic, offset_id = last.id, last.top_message
        # offset_date is the date of the last topic's last message.
        msg = next((m for m in page.messages if m.id == last.top_message), None)
        offset_date = last.date if getattr(page, "order_by_create_date", False) else (msg.date if msg else last.date)
        if len(seen) >= page.count:
            break
    return result


async def scan(client, config, store, history=False):
    dialog = await resolve_chat(client, config)
    if not config["topic_ids"] or len(config["topic_ids"]) != len(config["topic_names"]):
        raise SyncError("Сначала выполните configure для сохранения ID выбранных топиков.")
    subjects = dict(zip(config["topic_ids"], config["topic_names"], strict=True))
    cursors = {topic: 0 if history else store.cursor(dialog.id, topic) for topic in subjects}
    high = max(cursors.values(), default=0)
    since = cutoff(config)
    async for message in client.iter_messages(dialog.entity, min_id=min(cursors.values()), reverse=False):
        high = max(high, message.id)
        if message.date < since:
            break
        topic = topic_of(message)
        if topic not in subjects or message.id <= cursors[topic]:
            continue
        try:
            store.add(**recording_data(message, dialog.id, config))
        except SyncError:
            continue
    # Only commit high-water marks after an entire successful scan.
    for topic in subjects:
        store.advance(dialog.id, topic, high)


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
