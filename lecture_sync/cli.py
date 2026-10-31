from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import json
import os
import time
import uuid
import webbrowser
from pathlib import Path

from . import secrets, telegram
from .core import DEFAULT, Store, SyncError, config_path, cutoff, load_config, private_dir, run_lock, save_config, state_dir
from .lecturelog import AmbiguousConfirm, LectureLog


def output(value):
    print(json.dumps(value, ensure_ascii=False))


def remote(config):
    entry = secrets.read().get("lecturelog", {})
    if entry.get("base_url") != config["base_url"] or not entry.get("cookie"):
        raise SyncError("Сначала выполните login-web для этого сайта.")
    return LectureLog(config, entry["cookie"])


def login_web(config, manual=False, channel="chrome"):
    print("Войдите в LectureLog под отдельным аккаунтом. Пароль Google вводится только на странице Google.")
    if manual:
        webbrowser.open(config["base_url"] + "/upload")
        cookie = getpass.getpass("Значение ll_session из Cookies ТОЛЬКО сайта LectureLog: ").strip()
    else:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SyncError("Установите пакет с опцией [browser] либо используйте login-web --manual.") from None
        cookie = ""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, channel=channel or None)
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.goto(config["base_url"] + "/upload", wait_until="domcontentloaded")
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline and not page.is_closed():
                        cookies = context.cookies([config["base_url"] + "/"])
                        cookie = next((c["value"] for c in cookies if c["name"] == "ll_session"), "")
                        if cookie:
                            break
                        page.wait_for_timeout(1000)
                finally:
                    context.close()
                    browser.close()
        except Exception:
            raise SyncError("Не удалось завершить браузерный вход. Если Google отклоняет автоматизированный браузер, используйте login-web --manual в обычном браузере.") from None
    try:
        uuid.UUID(cookie)
    except (ValueError, TypeError):
        raise SyncError("Не получена корректная сессия LectureLog.") from None
    client = LectureLog(config, cookie)
    try:
        client.prepare()  # Read-only verification before persisting a credential.
        client.listings()
    finally:
        client.close()
    secrets.update(lecturelog=dict(base_url=config["base_url"], cookie=cookie))
    print("Сессия LectureLog сохранена в системной связке ключей.")


def reconcile(store, client, config, cards=None, directory=None):
    if cards is None:
        cards = client.listings()
    count = 0
    for row in store.rows(chat_id=config["chat_id"]):
        if row["status"] != "uncertain":
            continue
        matches = cards.get(row["title"], [])
        if len(matches) == 1:
            store.mark(row, "submitted", matches[0])
            count += 1
    if directory is not None:
        for row in store.rows("submitted", config["chat_id"]):
            telegram.download_path(row, directory / "downloads").unlink(missing_ok=True)
    return count


async def import_pending(config, store, directory):
    if not config["chat_id"] or not config["topic_ids"]:
        raise SyncError("Сначала выполните configure.")
    web = remote(config)
    try:
        await asyncio.to_thread(web.prepare)
        cards = await asyncio.to_thread(web.listings)
        reconcile(store, web, config, cards, directory)
        async with telegram.connected() as tg:
            dialog = await telegram.resolve_chat(tg, config)
            downloads = private_dir(directory / "downloads")
            for row in store.rows("pending", chat_id=dialog.id):
                if row["topic_id"] not in config["topic_ids"] or dt.datetime.fromisoformat(row["published_at"]) < cutoff(config):
                    continue
                if row["size"] > config["max_file_bytes"]:
                    continue
                # Recheck before every item, including after interrupted previous runs.
                cards = await asyncio.to_thread(web.listings)
                matches = cards.get(row["title"], [])
                if matches:
                    if len(matches) == 1:
                        store.mark(row, "submitted", matches[0])
                    else:
                        store.mark(row, "uncertain", error="Найдено несколько лекций с одинаковым заголовком.")
                    continue
                # Validate auth and host allowlist before downloading a large file.
                upload = await asyncio.to_thread(web.presign, row)
                path = await telegram.download(tg, dialog.entity, row, downloads)
                await asyncio.to_thread(web.put, path, row, upload)
                store.mark(row, "confirming")
                try:
                    await asyncio.to_thread(web.confirm, row, upload)
                    cards = await asyncio.to_thread(web.listings)
                    matches = cards.get(row["title"], [])
                    if len(matches) != 1:
                        raise AmbiguousConfirm("Лекция пока не найдена в списке после подтверждения.")
                except SyncError:
                    store.mark(row, "uncertain", error="Требуется сверка результата confirm.")
                    raise SyncError(f"Сообщение {row['message_id']}: результат неясен. Выполните reconcile; повтор заблокирован.") from None
                store.mark(row, "submitted", matches[0])
                path.unlink(missing_ok=True)
                print(f"Импортировано сообщение {row['message_id']}; lecture_id={matches[0]}")
    finally:
        web.close()


async def async_command(args, config, store, directory):
    if args.command == "login-telegram":
        await telegram.login()
        print("Telegram-сессия сохранена в системной связке ключей.")
        return
    if args.command in ("import", "sync"):
        if args.command == "sync":
            async with telegram.connected() as tg:
                await telegram.scan(tg, config, store)
        await import_pending(config, store, directory)
        return
    async with telegram.connected() as tg:
        if args.command == "dialogs":
            async for dialog in tg.iter_dialogs():
                if getattr(dialog.entity, "forum", False):
                    output(dict(id=dialog.id, name=dialog.name))
            return
        if args.command == "configure":
            if args.chat_id:
                config["chat_id"] = args.chat_id
            dialog = await telegram.resolve_chat(tg, config)
            all_topics = await telegram.topics(tg, dialog.entity)
            selected = []
            for name in config["topic_names"]:
                matches = [t for t in all_topics if t.title.casefold() == name.casefold()]
                if len(matches) != 1:
                    for t in all_topics:
                        output(dict(id=t.id, title=t.title))
                    raise SyncError("Название темы не найдено однозначно. Уточните topic_names в конфигурации.")
                selected.append(matches[0].id)
            config["chat_id"] = dialog.id
            config["topic_ids"] = selected
            save_config(args.config, config)
            output(dict(chat_id=dialog.id, topics=dict(zip(selected, config["topic_names"])) ))
        elif args.command == "topics":
            dialog = await telegram.resolve_chat(tg, config)
            for topic in await telegram.topics(tg, dialog.entity):
                output(dict(id=topic.id, title=topic.title))
        elif args.command == "scan":
            await telegram.scan(tg, config, store, history=args.history)


def parser():
    p = argparse.ArgumentParser(prog="lecturebot", description="LectureBot: импорт выбранных тем Telegram в LectureLog")
    p.add_argument("--config", type=Path, default=config_path())
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("init", "login-telegram", "dialogs", "topics", "preview", "import", "sync", "reconcile", "check"):
        sub.add_parser(command)
    c = sub.add_parser("configure")
    c.add_argument("--chat-id", type=int)
    s = sub.add_parser("scan")
    s.add_argument("--history", action="store_true", help="Заново просмотреть историю с даты since")
    w = sub.add_parser("login-web")
    w.add_argument("--manual", action="store_true")
    w.add_argument("--browser-channel", default="chrome", help="chrome; пустая строка — Chromium Playwright")
    r = sub.add_parser("resolve")
    r.add_argument("message_id", type=int)
    r.add_argument("--confirmed-absent", action="store_true", required=True,
                   help="Вы вручную проверили, что лекция/задача на сайте не создана")
    return p


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        directory = state_dir()
        with run_lock(directory):
            if args.command == "init":
                if args.config.exists():
                    raise SyncError("Конфигурация уже существует; перезапись отменена.")
                save_config(args.config, DEFAULT.copy())
                print(f"Создана конфигурация: {args.config}")
                return
            config = load_config(args.config)
            if args.command == "login-web":
                login_web(config, args.manual, args.browser_channel)
                return
            store = Store(directory / "state.sqlite3")
            try:
                if args.command == "preview":
                    for row in store.rows(chat_id=config["chat_id"] or None):
                        output({k: row[k] for k in ("message_id", "subject", "title", "size", "status", "lecture_id", "error")})
                elif args.command in ("check", "reconcile"):
                    web = remote(config)
                    try:
                        web.prepare()
                        count = reconcile(store, web, config, directory=directory)
                        print(f"LectureLog доступен. Сверено неоднозначных загрузок: {count}.")
                    finally:
                        web.close()
                elif args.command == "resolve":
                    matches = [r for r in store.rows("uncertain", config["chat_id"]) if r["message_id"] == args.message_id]
                    if len(matches) != 1:
                        raise SyncError("Не найдена неоднозначная загрузка с таким ID.")
                    store.mark(matches[0], "pending")
                    print("Запись возвращена в очередь после вашей ручной проверки.")
                else:
                    asyncio.run(async_command(args, config, store, directory))
            finally:
                store.close()
    except KeyboardInterrupt:
        print("Остановлено. Незавершённые подтверждения будут сверены при следующем запуске.")
        raise SystemExit(130)
    except (SyncError, ValueError, OSError) as exc:
        # Raw library exceptions can contain signed URLs, phone numbers or tokens.
        message = str(exc) if isinstance(exc, SyncError) else type(exc).__name__
        print("Ошибка: " + message)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Ошибка {type(exc).__name__}; подробности скрыты, чтобы не раскрывать секреты.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
