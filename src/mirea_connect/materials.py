"""Экспорт опубликованных материалов курсов СДО (online-edu.mirea.ru, Moodle) в ZIP.

Только локальный режим. Читаем HTML курса тем же httpx-клиентом, что у ``MireaSDO``,
и скачиваем лишь материалы преподавателя: resource, folder, page, url, assign (только
описание и приложенные к заданию файлы), label. Тесты, посещаемость, попытки, отправки
и отзывы не открываются вообще.

Правила запросов:
- GET только на ``https://online-edu.mirea.ru`` и только по белому списку путей
  (см. :func:`safe_sdo_url`); URL собирается заново, лишние параметры отбрасываются;
- редиректы не следуют автоматически: каждый ``Location`` проверяется тем же списком,
  уход на другой хост с cookies не делается, внешний адрес только записывается;
- страница входа (редирект на /login/ или SSO, форма логина в ответе) — сессия истекла,
  экспорт останавливается со статусом ``relogin_required``;
- размер каждого ответа ограничен, лимиты по курсу и по всему архиву явные; что не
  влезло, помечается в манифесте, а архив — как ``partial``.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import io
import json
import logging
import math
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SDO_HOST = "online-edu.mirea.ru"
SDO_ORIGIN = f"https://{SDO_HOST}"
MB = 1024 * 1024

# что скачиваем; всё прочее — не открываем
SUPPORTED = {"resource", "folder", "page", "url", "assign", "label", "lanebs"}
INTERACTIVE = {
    "quiz", "lesson", "workshop", "feedback", "choice", "attendance", "forum", "chat", "scorm",
    "h5pactivity", "hvp", "lti", "survey", "questionnaire", "data", "glossary", "wiki",
    "bigbluebutton", "bigbluebuttonbn", "zoom", "jitsi", "checklist", "journal", "offlinequiz",
    "adaptivequiz", "game", "imscp", "book",
}  # fmt: skip

_VIEW_PATHS = {"/course/view.php"} | {f"/mod/{m}/view.php" for m in ("resource", "folder", "page", "url", "assign", "lanebs")}
# (component, filearea) из /pluginfile.php/<ctx>/<component>/<filearea>/... — только материалы преподавателя
_PLUGINFILE_AREAS = {
    ("mod_resource", "content"),
    ("mod_folder", "content"),
    ("mod_page", "content"),
    ("mod_assign", "introattachment"),
    ("course", "section"),
    ("course", "summary"),
    ("course", "overviewfiles"),
} | {(f"mod_{m}", "intro") for m in ("resource", "folder", "page", "url", "assign", "label")}
_SENSITIVE_PARAM = re.compile(r"(?i)sess|token|key|pass|auth|sig|secret|ticket|code|state|session")
_SCRUB = re.compile(r"(?i)\b(sesskey|wstoken|token|logintoken|access_token|id_token|refresh_token|password|cookie|secret|ticket)\s*=\s*[^&\s\"'<>]+")


def scrub_text(value: str) -> str:
    """Detect encoded assignments without decoding unrelated text or URLs."""
    def encoded(match):
        original = match.group(0)
        decoded = original
        for _ in range(3):
            decoded = html_lib.unescape(unquote(decoded))
        return _SCRUB.sub(r"\1=<removed>", decoded) if _SCRUB.search(decoded) else original

    value = re.sub(r"[^\s<>\"']*%[0-9a-fA-F]{2}[^\s<>\"']*", encoded, str(value))
    return _SCRUB.sub(r"\1=<removed>", value)


def _scrub_data(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: v if k == "path" else redact_url(v) if (k == "url" or k.endswith("_url")) and isinstance(v, str) else _scrub_data(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_data(v) for v in value]
    return value


@dataclass(frozen=True)
class ExportLimits:
    max_file_bytes: int = 50 * MB
    max_course_bytes: int | None = None
    max_total_bytes: int | None = None
    max_metadata_bytes: int = 16 * MB
    max_html_bytes: int = 5 * MB
    max_items_per_course: int = 300
    max_files_per_folder: int = 200
    max_redirects: int = 5
    timeout_s: float = 60.0


@dataclass
class CourseRef:
    id: str
    name: str


class AuthRequired(Exception):
    """СДО вернула вход вместо данных — нужна новая сессия."""


class FetchError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ExternalRedirect(FetchError):
    def __init__(self, location: str) -> None:
        super().__init__("external_redirect", "редирект на неразрешённый адрес не выполнялся")
        self.location = location


# --- адреса ---


def _segments_ok(path: str) -> bool:
    for seg in path.split("/")[1:]:
        dec = unquote(seg)
        if dec in (".", "..") or "/" in dec or "\\" in dec or "\x00" in dec:
            return False
    return True


def safe_sdo_url(url: str, base: str = SDO_ORIGIN + "/") -> str | None:
    """Канонический адрес СДО, который можно запросить с cookies, иначе ``None``."""
    try:
        parts = urlsplit(urljoin(base, (url or "").strip()))
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or "@" in parts.netloc:
        return None
    if (parts.hostname or "").lower() != SDO_HOST or port not in (None, 443):
        return None
    path = parts.path
    if not _segments_ok(path):
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    if path in _VIEW_PATHS:
        allowed = {"id"} | ({"redirect"} if path == "/mod/resource/view.php" else set())
        if any(k not in allowed for k, _ in query):
            return None  # action=, attempt=, sesskey= и прочее — не наш случай
        ids = [v for k, v in query if k == "id"]
        if len(ids) != 1 or not ids[0].isdigit():
            return None
        q = [("id", ids[0])] + [("redirect", "1") for k, v in query if k == "redirect" and v == "1"][:1]
        return urlunsplit(("https", SDO_HOST, path, urlencode(q), ""))
    if path.startswith("/pluginfile.php/"):
        segs = path.split("/")  # ['', 'pluginfile.php', ctx, component, area, ...]
        if len(segs) < 6 or not segs[2].isdigit() or (segs[3], segs[4]) not in _PLUGINFILE_AREAS:
            return None
        q = [("forcedownload", "1")] if ("forcedownload", "1") in query else []
        return urlunsplit(("https", SDO_HOST, path, urlencode(q), ""))
    return None


def _is_login_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if host == SDO_HOST:
        return parts.path.startswith("/login/") or parts.path.startswith("/auth/")
    return host.startswith("sso.") and host.endswith("mirea.ru")


def redact_url(url: str) -> str:
    """Адрес для манифеста: без логина/пароля, фрагмента и параметров вроде sesskey/token."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    netloc = parts.hostname + (f":{port}" if port else "")
    q = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _SENSITIVE_PARAM.search(key):
            continue
        decoded = value
        for _ in range(3):
            decoded = html_lib.unescape(unquote(decoded))
        q.append((key, "removed" if _SCRUB.search(decoded) else value))
    # This is an output projection, never the authenticated download address.
    # Inspect each segment independently: preserve unrelated percent encodings
    # and path boundaries, remove only credential-bearing segment contents.
    segments = []
    for segment in parts.path.split("/"):
        decoded = segment
        for _ in range(3):
            decoded = html_lib.unescape(unquote(decoded))
        segments.append("removed" if _SCRUB.search(decoded) else segment)
    return urlunsplit((parts.scheme, netloc, "/".join(segments), urlencode(q), ""))


def _looks_like_login(body: bytes) -> bool:
    head = body[: 512 * 1024]
    return (
        b"page-login-index" in head
        or b"kc-form-login" in head
        or (b'name="logintoken"' in head and b'name="password"' in head)
    )


def _sniff_html(body: bytes) -> bool:
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _cd_filename(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]*)"', value) or re.search(r"filename\s*=\s*([^;]+)", value)
    return m.group(1).strip() if m else None


# --- имена в архиве ---

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 0 < len(ext) <= 10:
        return stem, "." + ext
    return name, ""


def safe_component(name: str, fallback: str = "file", max_len: int = 100) -> str:
    """Один компонент пути в ZIP: без разделителей, ``..``, управляющих символов и имён устройств Windows."""
    name = unicodedata.normalize("NFC", scrub_text(name or ""))
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if name.split(".")[0].strip().upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        stem, ext = _split_ext(name)
        name = stem[: max_len - len(ext)].rstrip(" .") + ext
    return name


class _Names:
    def __init__(self) -> None:
        self._used: set[str] = set()

    def unique(self, parts: list[str]) -> str:
        parts = [safe_component(p) for p in parts]
        dirs, leaf = parts[:-1], parts[-1]
        stem, ext = _split_ext(leaf)
        n = 1
        while True:
            cand = "/".join(dirs + [leaf if n == 1 else f"{stem} ({n}){ext}"])
            if cand.casefold() not in self._used:
                self._used.add(cand.casefold())
                return cand
            n += 1


# --- сеть ---


@dataclass
class Fetched:
    url: str
    content_type: str
    body: bytes
    filename: str | None

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml") or _sniff_html(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class SafeFetcher:
    """GET по белому списку СДО с ручной проверкой каждого редиректа и лимитом размера."""

    def __init__(self, client: httpx.AsyncClient, limits: ExportLimits) -> None:
        self.client = client
        self.limits = limits

    async def get(self, url: str, *, max_bytes: int) -> Fetched:
        current = safe_sdo_url(url)
        if current is None:
            raise FetchError("blocked_url", "адрес вне разрешённого списка СДО, не запрашивался")
        for _ in range(self.limits.max_redirects + 1):
            try:
                async with self.client.stream(
                    "GET", current, follow_redirects=False, timeout=self.limits.timeout_s
                ) as r:
                    if r.is_redirect:
                        nxt = urljoin(current, r.headers.get("location", ""))
                        if _is_login_url(nxt):
                            raise AuthRequired()
                        safe = safe_sdo_url(nxt)
                        if safe is None:
                            raise ExternalRedirect(redact_url(nxt))
                        current = safe
                        continue
                    if r.status_code == 401:
                        raise AuthRequired()
                    if r.status_code != 200:
                        raise FetchError("http_error", f"HTTP {r.status_code}")
                    length = r.headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise FetchError("too_large", f"{int(length)} байт больше лимита {max_bytes}")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise FetchError("too_large", f"больше лимита {max_bytes} байт")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    body = bytes(buf)
                    # форму входа ловим и под чужим content-type (например, «pdf»), чтобы не сохранить её как файл
                    if (ctype in ("text/html", "application/xhtml+xml", "") or _sniff_html(body)) and _looks_like_login(
                        body
                    ):
                        raise AuthRequired()
                    return Fetched(current, ctype, body, _cd_filename(r.headers.get("content-disposition")))
            except httpx.HTTPError as e:
                raise FetchError("network", f"сетевая ошибка: {type(e).__name__}") from None
        raise FetchError("too_many_redirects", "слишком много редиректов")


# --- разбор HTML ---


@dataclass
class Activity:
    cmid: str
    modtype: str
    name: str
    section: str
    url: str | None
    text: str = ""


def _soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup.find_all(["script", "style", "noscript", "iframe", "frame", "object", "embed", "template"]):
        t.decompose()
    for t in soup.select(".accesshide"):
        t.decompose()
    return soup


def _section_of(el) -> str:
    sec = el.find_parent(
        lambda t: t.name in ("li", "section", "div")
        and ("section" in (t.get("class") or []) or t.get("data-for") == "section")
        and t.name != "ul"
    )
    if sec is None:
        return ""
    title = sec.find(class_="sectionname")
    return (title.get_text(" ", strip=True) if title else "") or sec.get("aria-label", "") or ""


def parse_course(html: str) -> list[Activity]:
    soup = _soup(html)
    acts: list[Activity] = []
    seen: set[str] = set()
    for el in soup.find_all(class_=re.compile(r"^modtype_")):
        mod = next(c[len("modtype_") :] for c in el["class"] if c.startswith("modtype_"))
        m = re.fullmatch(r"module-(\d+)", el.get("id", ""))
        cmid = m.group(1) if m else str(el.get("data-id") or "")
        if not cmid.isdigit() or cmid in seen:
            continue
        seen.add(cmid)
        link = el.find("a", href=re.compile(rf"/mod/{re.escape(mod)}/view\.php"))
        name_el = el.find(class_="instancename")
        name = (
            el.get("data-activityname")
            or (name_el.get_text(" ", strip=True) if name_el else "")
            or (link.get_text(" ", strip=True) if link else "")
            or f"{mod} {cmid}"
        )
        text = ""
        if mod == "label":
            body = el.find(class_=re.compile(r"contentwithoutlink|activity-altcontent|description")) or el
            text = _SCRUB.sub(r"\1=<removed>", body.get_text("\n", strip=True))
            name = (text.splitlines() or [name])[0][:80] or name
        acts.append(Activity(cmid, mod, name, _section_of(el), link["href"] if link else None, text))
    return acts


def pluginfile_links(html: str, base_url: str, areas: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Ссылки на pluginfile из указанных областей: (канонический URL, текст ссылки)."""
    out, seen = [], set()
    for a in _soup(html).find_all("a", href=True):
        url = safe_sdo_url(urljoin(base_url, a["href"]))
        if not url or url in seen or not urlsplit(url).path.startswith("/pluginfile.php/"):
            continue
        segs = urlsplit(url).path.split("/")
        if (segs[3], segs[4]) in areas:
            seen.add(url)
            out.append((url, a.get_text(" ", strip=True)))
    return out


def _main_region(soup: BeautifulSoup):
    return soup.find(id="region-main") or soup.find(attrs={"role": "main"}) or soup.body or soup


def html_to_markdown(html: str, base_url: str, title: str, source_url: str, *, intro_only: bool = False) -> str:
    """Инертный Markdown: только текст и список ссылок, без скриптов и разметки."""
    soup = _soup(html)
    for t in soup.find_all(["form", "button", "input", "select", "textarea", "svg", "canvas"]):
        t.decompose()
    region = _main_region(soup)
    if intro_only:  # у задания — только описание преподавателя, без статуса отправки
        region = region.find(id="intro") or region.find(class_="activity-description")
    lines = [f"# {title}", "", f"Источник: {redact_url(source_url)}", ""]
    if region is None:
        lines.append("(описание не найдено)")
        return scrub_text("\n".join(lines)) + "\n"
    for t in region.find_all(class_=re.compile(r"submissionstatus|feedback|submissionsummary")):
        t.decompose()
    links = []
    for a in region.find_all("a", href=True):
        url = redact_url(urljoin(base_url, a["href"]))
        if url:
            links.append(f"- [{a.get_text(' ', strip=True) or url}]({url})")
    lines.append(_SCRUB.sub(r"\1=<removed>", region.get_text("\n", strip=True)))
    if links:
        lines += ["", "## Ссылки", *dict.fromkeys(links)]
    return scrub_text("\n".join(lines)) + "\n"


def _external_target(html: str, base_url: str) -> str | None:
    region = _main_region(_soup(html))
    for a in region.select(".urlworkaround a[href]") + region.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        if (urlsplit(url).hostname or "").lower() != SDO_HOST:
            return redact_url(url) or None
    return None


# --- экспорт ---


def _library_card(page: Fetched, title: str) -> tuple[dict, bytes]:
    """Read only Moodle's bibliography; never initialise the external reader."""
    from .page_snapshot import static_page

    region = _main_region(_soup(page.text()))
    entry = region.select_one(".item[data-id]")
    citation = region.select_one(".biblio_record")
    if entry is None or entry.get("data-type") != "book":
        raise FetchError("unrecognized_library_metadata", "страница не содержит распознанной карточки библиотеки")
    if not re.fullmatch(r"[1-9][0-9]{0,15}", str(entry.get("data-id") or "")):
        raise FetchError("invalid_library_id", "карточка библиотеки не содержит корректного идентификатора книги")
    metadata = {
        "title": scrub_text(title),
        "bibliography": scrub_text(citation.get_text(" ", strip=True)) if citation else "",
        "source_url": redact_url(page.url),
        "book_id": None,
        "page": None,
    }
    if entry:
        book_id = str(entry.get("data-id") or "")
        page_number = str(entry.get("data-page") or "")
        if entry.get("data-type") == "book" and re.fullmatch(r"[1-9][0-9]{0,15}", book_id):
            metadata["book_id"] = book_id
        if re.fullmatch(r"[1-9][0-9]{0,6}", page_number):
            metadata["page"] = int(page_number)
    target = f"https://e.lanbook.com/book/{metadata['book_id']}" if metadata["book_id"] else metadata["source_url"]
    metadata["external_url"] = target
    esc = html_lib.escape
    bibliography = esc(metadata["bibliography"] or "Библиографическая запись не указана.")
    number = str(metadata["page"]) if metadata["page"] is not None else "не указана"
    markup = (
        '<main id="region-main"><p><strong>ЭБС Лань: метаданные и ссылка.</strong></p>'
        f'<p>{bibliography}</p><p>Страница: {number}</p>'
        f'<p><a href="{esc(target, quote=True)}">Открыть ресурс</a></p>'
        '<p>Полный текст не скачивался. Для чтения может потребоваться вход в библиотеку.</p></main>'
    )
    return metadata, static_page(markup, title, metadata["source_url"])


@dataclass
class ExportResult:
    zip_bytes: bytes
    manifest: dict
    status: str


_PROBLEM = {"failed", "partial", "limit_reached", "too_large", "cancelled", "unsupported"}


@dataclass
class _Budget:
    course_left: float
    total_left: float
    limits: ExportLimits

    def file_max(self) -> int:
        return int(min(self.limits.max_file_bytes, self.course_left, self.total_left))

    def spend(self, n: int) -> None:
        self.course_left -= n
        self.total_left -= n


@dataclass
class _Exporter:
    fetcher: SafeFetcher
    limits: ExportLimits
    cancel: asyncio.Event
    progress: Callable[[dict], None]
    zf: zipfile.ZipFile
    names: _Names = field(default_factory=_Names)
    total_left: float = 0
    saved_files: int = 0
    saved_bytes: int = 0
    metadata_bytes: int = 0

    def save(self, parts: list[str], data: bytes, *, budget: _Budget | None = None) -> str:
        # Validate before writing: no generated Markdown/HTML bypasses the budget.
        if budget is not None and len(data) > budget.file_max():
            raise FetchError("limit_reached", "материал не влезает в установленный лимит")
        if budget is None and self.metadata_bytes + len(data) > self.limits.max_metadata_bytes:
            raise FetchError("metadata_limit", "исчерпан отдельный лимит служебных метаданных")
        name = self.names.unique(parts)
        self.zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        if budget is not None:
            self.saved_files += 1
            self.saved_bytes += len(data)
            budget.spend(len(data))
        else:
            self.metadata_bytes += len(data)
        return name

    async def get_bounded(self, url: str, budget: _Budget) -> Fetched:
        cap = budget.file_max()
        if cap <= 0:
            raise FetchError("limit_reached", "исчерпан лимит размера курса или архива, не скачивалось")
        try:
            return await self.fetcher.get(url, max_bytes=cap)
        except FetchError as e:
            if e.code == "too_large" and cap < self.limits.max_file_bytes:
                raise FetchError("limit_reached", f"не влезло в оставшийся лимит курса/архива: {e.message}") from None
            raise

    async def fetch_file(self, url: str, budget: _Budget) -> Fetched:
        f = await self.get_bounded(url, budget)
        if f.is_html:
            raise FetchError("unexpected_html", "вместо файла пришла HTML-страница, не сохранено")
        return f

    def store_file(self, f: Fetched, dirs: list[str], budget: _Budget, fallback: str) -> dict:
        fname = f.filename or unquote(urlsplit(f.url).path.rsplit("/", 1)[-1]) or fallback
        fname = fname.replace("\\", "/").rsplit("/", 1)[-1] or fallback  # имя от сервера — только basename
        path = self.save(dirs + [fname], f.body, budget=budget)
        return {"status": "saved", "path": path, "size": len(f.body), "file_url": redact_url(f.url)}

    async def snapshot(self, page: Fetched, parts: list[str], budget: _Budget, title: str, *, intro_only: bool = False, links: dict | None = None, files: list | None = None) -> dict:
        from .page_snapshot import image_sources, static_page

        links = dict(links or {})
        for entry in files or []:
            if entry.get("file_url") and entry.get("path"):
                links[entry["file_url"]] = posixpath.relpath(entry["path"], "/".join(parts[:-1]))
        cap = budget.file_max()
        images, omissions = {}, []
        used = len(page.body)
        for index, source in enumerate(image_sources(page.text(), page.url, intro_only)):
            if self.cancel.is_set():
                omissions.append({"url": redact_url(source), "reason": "cancelled"})
                break
            safe = safe_sdo_url(source)
            if not safe or not urlsplit(safe).path.startswith("/pluginfile.php/"):
                omissions.append({"url": redact_url(source), "reason": "image outside instructor-file allowlist"})
                continue
            remaining = min(cap - used, self.limits.max_html_bytes)
            if index >= self.limits.max_files_per_folder or remaining <= 0:
                omissions.append({"url": redact_url(source), "reason": "image size/count limit"})
                continue
            try:
                image = await self.fetcher.get(safe, max_bytes=remaining)
                if image.content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise FetchError("image_type", "unsupported image type")
                encoded = base64.b64encode(image.body).decode("ascii")
                images[source] = f"data:{image.content_type};base64,{encoded}"
                used += len(encoded)
            except FetchError as exc:
                omissions.append({"url": redact_url(source), "reason": exc.code})
        body = static_page(page.text(), title, page.url, intro_only=intro_only, images=images, links=links)
        if len(body) > cap:
            return {"status": "limit_reached", "error": "HTML-снимок не влезает в лимит курса/архива", "omissions": omissions}
        path = self.save(parts, body, budget=budget)
        return {"status": "partial" if omissions else "saved", "path": path, "size": len(body), "images": len(images), "omissions": omissions}

    async def item(self, act: Activity, course_dir: str, budget: _Budget, index: list[str]) -> dict:
        section = act.section or "Без раздела"
        dirs = [course_dir, section]
        item: dict[str, Any] = {
            "cmid": act.cmid,
            "type": act.modtype,
            "title": act.name,
            "section": act.section,
            "url": redact_url(urljoin(SDO_ORIGIN + "/", act.url)) if act.url else None,
            "status": "",
        }
        if act.modtype in INTERACTIVE:
            item.update(status="skipped_interactive", error="интерактивный элемент, не открывался")
            return item
        if act.modtype not in SUPPORTED:
            item.update(status="unsupported", error="тип элемента не поддерживается, не открывался")
            return item
        if act.modtype == "label":
            index.append(act.text)
            item.update(status="inline", path=f"{course_dir}/README.md")
            return item
        if not act.url:
            item.update(status="failed", error="у элемента нет ссылки")
            return item
        try:
            if act.modtype == "lanebs":
                page = await self.fetcher.get(act.url, max_bytes=self.limits.max_html_bytes)
                if not page.is_html:
                    raise FetchError("unexpected_content", "вместо метаданных библиотеки получен не HTML")
                metadata, card = _library_card(page, act.name)
                if len(card) > budget.file_max():
                    raise FetchError("limit_reached", "карточка библиотеки не влезает в лимит курса/архива")
                path = self.save(dirs + [act.name + ".html"], card, budget=budget)
                item.update(status="metadata_only", metadata=metadata, external_url=metadata["external_url"],
                            fulltext_downloaded=False, path=path, size=len(card))
            elif act.modtype == "resource":
                f = await self.get_bounded(act.url, budget)
                landing = f if f.is_html else None
                if f.is_html:  # ресурс показан страницей — ищем ссылку на сам файл
                    links = pluginfile_links(f.text(), f.url, {("mod_resource", "content")})
                    if not links:
                        raise FetchError("no_file_link", "на странице ресурса нет ссылки на файл")
                    f = await self.fetch_file(links[0][0], budget)
                item.update(self.store_file(f, dirs, budget, act.name))
                if landing:
                    item["page_snapshot"] = await self.snapshot(landing, dirs + [act.name + ".html"], budget, act.name, files=[item])
            elif act.modtype == "folder":
                page = await self.fetcher.get(act.url, max_bytes=self.limits.max_html_bytes)
                await self.files_into(item, pluginfile_links(page.text(), page.url, {("mod_folder", "content")}),
                                      dirs + [act.name], budget, folder=True)  # fmt: skip
                item["page_snapshot"] = await self.snapshot(page, dirs + [act.name, "index.html"], budget, act.name, files=item.get("files"))
            elif act.modtype == "page":
                page = await self.fetcher.get(act.url, max_bytes=self.limits.max_html_bytes)
                md = html_to_markdown(page.text(), page.url, act.name, page.url).encode()
                item.update(status="saved", path=self.save(dirs + [act.name + ".md"], md, budget=budget), size=len(md))
                item["page_snapshot"] = await self.snapshot(page, dirs + [act.name + ".html"], budget, act.name)
            elif act.modtype == "assign":
                page = await self.fetcher.get(act.url, max_bytes=self.limits.max_html_bytes)
                md = html_to_markdown(page.text(), page.url, act.name, page.url, intro_only=True).encode()
                item.update(status="saved", path=self.save(dirs + [act.name, "Задание.md"], md, budget=budget), size=len(md))
                links = pluginfile_links(page.text(), page.url, {("mod_assign", "introattachment")})
                await self.files_into(item, links, dirs + [act.name], budget, folder=False)
                item["page_snapshot"] = await self.snapshot(page, dirs + [act.name, "index.html"], budget, act.name, intro_only=True, files=item.get("files"))
            elif act.modtype == "url":
                try:
                    page = await self.fetcher.get(act.url, max_bytes=self.limits.max_html_bytes)
                    target = _external_target(page.text(), page.url)
                except ExternalRedirect as e:
                    target = e.location
                if not target:
                    raise FetchError("no_link", "внешний адрес не найден")
                item.update(status="external_link", external_url=target)
        except AuthRequired:
            raise
        except FetchError as e:
            item.update(status=e.code if e.code in ("limit_reached", "too_large") else "failed", error=e.message)
        except OSError:
            raise  # Disk-full/write errors are not an ordinary unsupported item.
        except Exception as e:  # разметка неожиданная — помечаем, идём дальше
            logger.warning("materials item %s failed: %s", act.cmid, type(e).__name__)
            item.update(status="failed", error=f"ошибка обработки: {type(e).__name__}")
        if item.get("page_snapshot", {}).get("status", "saved") != "saved" and item["status"] == "saved":
            item["status"] = "partial"
        return item

    async def files_into(self, item: dict, links: list, dirs: list[str], budget: _Budget, *, folder: bool) -> None:
        files = []
        for n, (url, label) in enumerate(links):
            if self.cancel.is_set():
                files.append({"url": redact_url(url), "status": "cancelled"})
                continue
            if n >= self.limits.max_files_per_folder:
                files.append({"url": redact_url(url), "status": "limit_reached", "error": "лимит файлов в папке"})
                continue
            sub = []
            if folder:  # /pluginfile.php/<ctx>/mod_folder/content/<itemid>/<подпапки>/<файл>
                sub = [unquote(s) for s in urlsplit(url).path.split("/")[6:-1] if s]
            try:
                f = await self.fetch_file(url, budget)
                files.append(self.store_file(f, dirs + sub, budget, label or "file"))
            except FetchError as e:
                st = e.code if e.code in ("limit_reached", "too_large") else "failed"
                files.append({"url": redact_url(url), "status": st, "error": e.message})
        item["files"] = files
        if item.get("status") != "saved" and not files:
            item.update(status="failed", error="файлов не найдено")
        elif any(f["status"] != "saved" for f in files):
            item["status"] = "partial"
        elif not item.get("status"):
            item["status"] = "saved"


def _course_readme(course: dict, labels: list[str]) -> str:
    lines = [f"# {course['name']}", "", f"Курс СДО {course['id']}: {course['url']}", f"Статус: {course['status']}", ""]
    if course.get("error"):
        lines += [f"Ошибка: {course['error']}", ""]
    if course.get("page_snapshot", {}).get("path"):
        lines += [f"Открыть снимок курса: {course['page_snapshot']['path'].rsplit('/', 1)[-1]}", ""]
    section = None
    for it in course["items"]:
        if it["section"] != section:
            section = it["section"]
            lines += ["", f"## {section or 'Без раздела'}"]
        where = it.get("path") or it.get("external_url") or it.get("url") or ""
        err = f" — {it['error']}" if it.get("error") else ""
        lines.append(f"- [{it['status']}] {it['title']} ({it['type']}) {where}{err}")
        if it.get("status") == "metadata_only":
            lines.append("  - Только метаданные и ссылка; полный текст не скачивался.")
            lines.append(f"  - Открыть ресурс: {it['external_url']}")
        for f in it.get("files") or []:
            lines.append(f"  - [{f['status']}] {f.get('path') or f.get('url')}{' — ' + f['error'] if f.get('error') else ''}")
    if labels:
        lines += ["", "## Текстовые блоки курса", *[f"\n{scrub_text(t)}" for t in labels]]
    return "\n".join(lines) + "\n"


async def _export_archive(
    client: httpx.AsyncClient,
    courses: list[CourseRef],
    *,
    scope: str,
    limits: ExportLimits,
    cancel: asyncio.Event,
    progress: Callable[[dict], None],
    zf: zipfile.ZipFile,
) -> ExportResult:
    ex = _Exporter(SafeFetcher(client, limits), limits, cancel, progress, zf)
    ex.total_left = limits.max_total_bytes if limits.max_total_bytes is not None else math.inf
    out_courses: list[dict] = []
    aborted = None

    for ci, ref in enumerate(courses):
        cid = str(ref.id)
        url = f"{SDO_ORIGIN}/course/view.php?id={cid}"
        course = {"id": cid, "name": scrub_text(ref.name), "url": url, "status": "", "items": [], "bytes": 0}
        out_courses.append(course)
        if aborted or cancel.is_set():
            course.update(status="cancelled" if not aborted else "not_started", error="экспорт остановлен")
            continue
        course_dir = safe_component(f"{cid} {ref.name}", fallback=cid)
        budget = _Budget(min(limits.max_course_bytes if limits.max_course_bytes is not None else math.inf, ex.total_left), ex.total_left, limits)
        start_bytes = ex.saved_bytes
        labels: list[str] = []
        progress({"course_index": ci + 1, "courses_total": len(courses), "course": ref.name, "items_done": 0})
        try:
            page = await ex.fetcher.get(url, max_bytes=limits.max_html_bytes)
            acts = parse_course(page.text()) if page.is_html else []
            if not acts:
                raise FetchError("empty", "на странице курса не найдено ни одного элемента (нет доступа или разметка не распознана)")
            for n, act in enumerate(acts):
                if cancel.is_set():
                    course["items"].append({"cmid": act.cmid, "type": act.modtype, "title": act.name,
                                            "section": act.section, "status": "cancelled"})  # fmt: skip
                    continue
                if n >= limits.max_items_per_course:
                    course["items"].append({"cmid": act.cmid, "type": act.modtype, "title": act.name,
                                            "section": act.section, "status": "limit_reached",
                                            "error": "лимит элементов курса"})  # fmt: skip
                    continue
                course["items"].append(await ex.item(act, course_dir, budget, labels))
                progress({"course_index": ci + 1, "courses_total": len(courses), "course": ref.name,
                          "items_done": n + 1, "items_total": len(acts), "bytes": ex.saved_bytes})  # fmt: skip
            bad = [i for i in course["items"] if i["status"] in _PROBLEM]
            offline_links = {}
            for item in course["items"]:
                target = item.get("page_snapshot", {}).get("path") or item.get("path")
                if item.get("url") and target:
                    offline_links[item["url"]] = posixpath.relpath(target, course_dir)
            course["page_snapshot"] = await ex.snapshot(page, [course_dir, "course.html"], budget, ref.name, links=offline_links)
            snapshot_bad = course["page_snapshot"]["status"] != "saved"
            course["status"] = "cancelled" if cancel.is_set() else ("partial" if bad or snapshot_bad else "complete")
        except AuthRequired:
            aborted = "relogin_required"
            course.update(status="failed", error="СДО запросила вход заново — сессия истекла")
        except FetchError as e:
            course.update(status="failed", error=e.message)
        course["bytes"] = ex.saved_bytes - start_bytes
        ex.total_left = budget.total_left
        try:
            ex.save([course_dir, "README.md"], _course_readme(_scrub_data(course), labels).encode())
        except FetchError:
            course.update(status="partial", error="служебный README не влезает в лимит метаданных")

    ok = [c for c in out_courses if c["status"] in ("complete", "partial")]
    if aborted:
        status = aborted
    elif cancel.is_set():
        status = "cancelled"
    elif not out_courses or not ok:
        status = "failed"
    elif all(c["status"] == "complete" for c in out_courses):
        status = "complete"
    else:
        status = "partial"
    manifest = {
        "format": "mirea-connect-materials/1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": scope,
        "status": status,
        "limits": asdict(limits),
        "totals": {
            "courses": len(out_courses),
            "courses_ok": len(ok),
            "saved_files": ex.saved_files,
            "saved_bytes": ex.saved_bytes,
            "metadata_only": sum(i["status"] == "metadata_only" for c in out_courses for i in c["items"]),
        },
        "notes": [
            "Скачаны только опубликованные материалы преподавателя (resource, folder, page, url, assign-описание, label).",
            "Тесты, посещаемость, отправки и отзывы не открывались. Внешние ссылки только записаны.",
            "ЭБС Лань (lanebs): сохранены только метаданные, страница и ссылка; полные тексты не запрашивались.",
            "course.html и HTML страниц материалов — статические снимки; скрипты/формы отключены, пропуски изображений перечислены в page_snapshot.omissions.",
            "status=complete означает, что все поддерживаемые элементы сохранены; иначе смотрите items.",
        ],
        "courses": out_courses,
    }
    manifest = _scrub_data(manifest)
    manifest["byte_budget"] = {
        "unit": "uncompressed_material_bytes",
        "material_bytes": ex.saved_bytes,
        "readme_bytes": ex.metadata_bytes,
        "metadata_reserve_bytes": limits.max_metadata_bytes,
        "metadata_includes": ["course_README", "manifest.json"],
        "physical_zip_size_limited": False,
    }
    ex.save(["manifest.json"], json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    return ExportResult(b"", manifest, status)


async def export_materials(
    client: httpx.AsyncClient,
    courses: list[CourseRef],
    *,
    scope: str,
    limits: ExportLimits | None = None,
    cancel: asyncio.Event | None = None,
    progress: Callable[[dict], None] | None = None,
    output: BinaryIO | None = None,
) -> ExportResult:
    """Write ZIP incrementally to output; bytes compatibility is for small callers.

    The caller owns the output stream. ZIP central directory is finalized even
    on cancellation/errors; completed entries remain available, never erased.
    Response memory is bounded per item, not by the accumulated archive size.
    Optional byte budgets cover uncompressed materials, with a separately
    bounded service-metadata reserve; they are not a physical ZIP-size claim.
    """
    limits = limits or ExportLimits()
    for name in ("max_file_bytes", "max_html_bytes", "max_metadata_bytes", "max_items_per_course", "max_files_per_folder"):
        if getattr(limits, name) <= 0:
            raise ValueError("Лимиты размера и количества должны быть положительными")
    for name in ("max_course_bytes", "max_total_bytes"):
        if getattr(limits, name) is not None and getattr(limits, name) < 0:
            raise ValueError("Необязательный лимит не может быть отрицательным")
    buffer = io.BytesIO() if output is None else None
    with zipfile.ZipFile(buffer if buffer is not None else output, "w", allowZip64=True) as archive:
        result = await _export_archive(client, courses, scope=scope, limits=limits,
                                       cancel=cancel or asyncio.Event(), progress=progress or (lambda p: None), zf=archive)
    if buffer is not None:
        result.zip_bytes = buffer.getvalue()
    return result
