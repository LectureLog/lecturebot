from __future__ import annotations

import getpass
import inspect
import mimetypes
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from . import secrets
from .core import SyncError, cutoff, lecture_title, topic_of

EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


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
        file = message.file
        if not file:
            continue
        mime = file.mime_type or ""
        filename = file.name or f"recording-{message.id}{mimetypes.guess_extension(mime) or ''}"
        filename = Path(filename.replace("\\", "/")).name
        ext = Path(filename).suffix.lower()
        if ext not in EXTENSIONS:
            continue
        if not file.size or file.size > config["max_file_bytes"]:
            print(f"Пропущено сообщение {message.id}: неподходящий размер файла.")
            continue
        # Telegram documents sometimes report application/octet-stream.
        if not mime.startswith(("video/", "audio/")):
            mime = mimetypes.guess_type(filename)[0] or ("video/mp4" if ext in {".mp4", ".mkv", ".mov", ".avi", ".webm"} else "audio/mpeg")
        store.add(chat_id=dialog.id, message_id=message.id, topic_id=topic, subject=subjects[topic],
                  filename=filename, size=file.size, mime=mime, published_at=message.date.isoformat(),
                  title=lecture_title(subjects[topic], message.date.astimezone(since.tzinfo), filename, dialog.id, message.id))
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
