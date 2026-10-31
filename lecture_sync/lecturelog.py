from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .core import SyncError


class AuthExpired(SyncError):
    pass


class AmbiguousConfirm(SyncError):
    pass


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.csrf = None
        self.cards = {}
        self.article = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and a.get("id") == "ll-upload":
            self.csrf = a.get("data-csrf")
        if tag == "article" and a.get("id", "").startswith("lec-"):
            self.article = a["id"][4:]
        if tag == "input" and a.get("name") == "title" and self.article:
            self.cards.setdefault(a.get("value", ""), []).append(self.article)

    def handle_endtag(self, tag):
        if tag == "article":
            self.article = None


class LectureLog:
    def __init__(self, config, cookie, *, session=None, storage=None):
        self.config = config
        self.base = config["base_url"]
        self.http = session or requests.Session()
        self.storage = storage or requests.Session()
        # Do not inherit netrc credentials, environment proxies or ambient auth.
        self.http.trust_env = False
        self.storage.trust_env = False
        self.http.cookies.set("ll_session", cookie, domain=urlsplit(self.base).hostname, path="/", secure=True)
        self.csrf = None

    def close(self):
        self.http.close()
        self.storage.close()

    def request(self, method, path, **kwargs):
        try:
            r = self.http.request(method, self.base + path, timeout=(20, 120), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SyncError("LectureLog: ошибка соединения; секреты и URL загрузки скрыты.") from None
        if r.status_code in (301, 302, 303, 307, 308, 401):
            raise AuthExpired("Сессия LectureLog недействительна. Выполните login-web.")
        if r.status_code != 200:
            raise SyncError(f"LectureLog: {path} вернул HTTP {r.status_code}.")
        return r

    def prepare(self):
        page = Page(self.request("GET", "/upload").text)
        if not page.csrf:
            raise SyncError("Не найдена форма загрузки/CSRF: версия сайта отличается или требуется вход.")
        self.csrf = page.csrf

    def headers(self):
        if not self.csrf:
            raise SyncError("Сначала требуется получить CSRF-токен.")
        return {"X-CSRF-Token": self.csrf, "Origin": self.base, "Referer": self.base + "/upload"}

    def listings(self):
        html = self.request("GET", "/lectures").text
        if "ll-lectures-section" not in html:
            raise SyncError("Не распознана страница списка лекций; автоматическая сверка остановлена.")
        return Page(html).cards

    def presign(self, row):
        r = self.request("POST", "/upload/presign", headers=self.headers(), json={
            "filename": row["filename"], "size": row["size"], "mime": row["mime"],
        })
        try:
            data = r.json()
            for key in ("put_url", "token", "s3_key"):
                if not isinstance(data[key], str) or not data[key]:
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise SyncError("LectureLog вернул неизвестный формат presign.") from None
        target = urlsplit(data["put_url"])
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.fragment or target.netloc not in self.config["upload_hosts"]):
            host = target.hostname or "неизвестен"
            raise SyncError(f"Хост хранилища не разрешён: {host}. Проверьте его и добавьте в upload_hosts конфигурации.")
        return data

    def put(self, path: Path, row, upload):
        # A separate session prevents forwarding LectureLog cookies/CSRF to storage.
        try:
            with path.open("rb") as f:
                r = self.storage.put(upload["put_url"], data=f, headers={
                    "Content-Type": row["mime"], "Content-Length": str(row["size"]),
                }, timeout=(20, 600), allow_redirects=False)
        except requests.RequestException:
            raise SyncError("Ошибка передачи файла в хранилище; можно повторить импорт.") from None
        if r.status_code not in (200, 201, 204):
            raise SyncError(f"Хранилище вернуло HTTP {r.status_code}.")

    def confirm(self, row, upload):
        try:
            r = self.request("POST", "/upload/confirm", headers=self.headers(), data={
                "token": upload["token"], "s3_key": upload["s3_key"], "title": row["title"],
                "extract_slides": "true" if self.config["extract_slides"] else "",
            })
            if r.headers.get("HX-Redirect") != "/lectures":
                raise SyncError("Не распознано подтверждение загрузки.")
        except SyncError:
            # Even HTTP 500 can occur after the core has accepted the job.
            raise AmbiguousConfirm("Результат подтверждения неизвестен; автоматический повтор заблокирован до сверки.") from None
