"""Экспорт материалов СДО (CLI/MCP без отдельной панели).

Всё здесь — синтетика: HTML Moodle и ответы СДО придуманы для тестов (httpx.MockTransport),
сессия — в памяти. Это не проверка живой интеграции с online-edu.mirea.ru.
Хранилище-заглушка падает на ``clear()``: новые сценарии не должны удалять сессию.
"""

from __future__ import annotations

import io
import zipfile

import httpx

from mirea_connect.materials import (
    CourseRef,
    ExportLimits,
    export_materials,
    redact_url,
    safe_component,
    safe_sdo_url,
)

H = "https://online-edu.mirea.ru"
COOKIE = "MOODLE-SECRET-COOKIE-VALUE"
SESSKEY = "SESSKEY123abc"

COURSE1 = f"""<html><body id="page-course-view-topics">
<a href="{H}/login/logout.php?sesskey={SESSKEY}">Выход</a>
<ul class="topics"><li class="section main" id="section-1" aria-label="Лекции">
<h3 class="sectionname">Лекции</h3><ul class="section">
<li class="activity resource modtype_resource" id="module-11"><a href="{H}/mod/resource/view.php?id=11"><span class="instancename">Лекция 1<span class="accesshide"> Файл</span></span></a></li>
<li class="activity folder modtype_folder" id="module-12"><a href="{H}/mod/folder/view.php?id=12"><span class="instancename">Папка</span></a></li>
<li class="activity page modtype_page" id="module-13"><a href="{H}/mod/page/view.php?id=13"><span class="instancename">Страница</span></a></li>
<li class="activity url modtype_url" id="module-14"><a href="{H}/mod/url/view.php?id=14"><span class="instancename">Ссылка</span></a></li>
<li class="activity quiz modtype_quiz" id="module-15"><a href="{H}/mod/quiz/view.php?id=15"><span class="instancename">Тест</span></a></li>
<li class="activity assign modtype_assign" id="module-16"><a href="{H}/mod/assign/view.php?id=16"><span class="instancename">Задание</span></a></li>
<li class="activity label modtype_label" id="module-17"><div class="contentwithoutlink"><p>Объявление для группы</p></div></li>
<li class="activity attendance modtype_attendance" id="module-18"><a href="{H}/mod/attendance/view.php?id=18"><span class="instancename">Посещаемость</span></a></li>
<li class="activity mystery modtype_mystery" id="module-19"><a href="{H}/mod/mystery/view.php?id=19"><span class="instancename">Что-то</span></a></li>
</ul></li></ul></body></html>"""

FOLDER = f"""<html><body><div id="region-main">
<a href="{H}/pluginfile.php/101/mod_folder/content/0/CON.pdf?forcedownload=1">CON.pdf</a>
<a href="{H}/pluginfile.php/101/mod_folder/content/0/%D0%BF%D0%BE%D0%B4/notes.txt">notes.txt</a>
<a href="{H}/pluginfile.php/103/assignsubmission_file/submission_files/5/my.pdf">чужое</a>
<form method="post" action="{H}/mod/folder/download_folder.php"><input name="sesskey" value="{SESSKEY}"></form>
</div></body></html>"""

PAGE = f"""<html><body><div id="region-main"><script>alert(1)</script>
<h2>Тема</h2><p>Текст страницы</p><a href="{H}/mod/forum/view.php?id=3&sesskey={SESSKEY}">форум</a>
</div></body></html>"""

ASSIGN = f"""<html><body><div id="region-main">
<div id="intro"><p>Сделайте отчёт</p>
<a href="{H}/pluginfile.php/102/mod_assign/introattachment/0/task.pdf">task.pdf</a></div>
<table class="submissionstatustable"><tr><td>Отправлено</td><td>
<a href="{H}/pluginfile.php/103/assignsubmission_file/submission_files/5/my.pdf">my.pdf</a></td></tr></table>
</div></body></html>"""

COURSE2 = f"""<html><body><li class="section main" aria-label="Тема 1"><h3 class="sectionname">Тема 1</h3>
<li class="activity modtype_resource" id="module-21"><a href="{H}/mod/resource/view.php?id=21"><span class="instancename">А</span></a></li>
<li class="activity modtype_resource" id="module-22"><a href="{H}/mod/resource/view.php?id=22"><span class="instancename">Б</span></a></li>
</li></body></html>"""

LOGIN_HTML = b'<html><body id="page-login-index"><form id="login"><input name="logintoken"><input name="password"></form></body></html>'


def _pdf(name: str, body: bytes = b"%PDF-1.4 synthetic") -> httpx.Response:
    from urllib.parse import quote

    return httpx.Response(
        200, content=body, headers={"content-type": "application/pdf", "content-disposition": "attachment; filename*=UTF-8''" + quote(name)}
    )


def _html(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/html; charset=utf-8"})


def _redirect(loc: str) -> httpx.Response:
    return httpx.Response(303, headers={"location": loc})


def default_routes() -> dict:
    return {
        "/course/view.php?id=1": lambda: _html(COURSE1),
        "/mod/resource/view.php?id=11": lambda: _redirect(f"{H}/pluginfile.php/100/mod_resource/content/1/lec.pdf"),
        "/pluginfile.php/100/mod_resource/content/1/lec.pdf": lambda: _pdf("../../CON.pdf"),
        "/mod/folder/view.php?id=12": lambda: _html(FOLDER),
        "/pluginfile.php/101/mod_folder/content/0/CON.pdf?forcedownload=1": lambda: _pdf("CON.pdf"),
        "/pluginfile.php/101/mod_folder/content/0/%D0%BF%D0%BE%D0%B4/notes.txt": lambda: httpx.Response(
            200, content=b"notes", headers={"content-type": "text/plain"}
        ),
        "/mod/page/view.php?id=13": lambda: _html(PAGE),
        "/mod/url/view.php?id=14": lambda: _redirect("https://external.example/doc?token=abc&x=1"),
        "/mod/assign/view.php?id=16": lambda: _html(ASSIGN),
        "/pluginfile.php/102/mod_assign/introattachment/0/task.pdf": lambda: _pdf("task.pdf"),
        "/course/view.php?id=2": lambda: _html(COURSE2),
        "/mod/resource/view.php?id=21": lambda: _pdf("Отчёт:1.pdf"),
        "/mod/resource/view.php?id=22": lambda: _pdf("Отчёт:1.pdf"),
    }


class Net:
    """Синтетический СДО: записывает каждый запрос, неизвестные адреса — 404."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes if routes is not None else default_routes()
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.host != "online-edu.mirea.ru":
            return httpx.Response(200, text="SHOULD NOT BE FETCHED")
        key = request.url.raw_path.decode()
        fn = self.routes.get(key)
        return fn() if fn else httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), cookies={"MoodleSession": COOKIE})

    def paths(self) -> list[str]:
        return [str(r.url) for r in self.seen]


def _zip(result) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as z:
        return {n: z.read(n) for n in z.namelist()}


def _items(manifest: dict, course: str = "1") -> dict[str, dict]:
    c = next(c for c in manifest["courses"] if c["id"] == course)
    return {i["cmid"]: i for i in c["items"]}


# --- адреса и имена ---


def test_safe_sdo_url_whitelist():
    assert safe_sdo_url(f"{H}/course/view.php?id=5#section-2") == f"{H}/course/view.php?id=5"
    assert safe_sdo_url("/mod/resource/view.php?id=7") == f"{H}/mod/resource/view.php?id=7"
    assert safe_sdo_url(f"{H}/pluginfile.php/1/mod_resource/content/0/a.pdf?token=x&forcedownload=1").endswith(
        "a.pdf?forcedownload=1"
    )
    for bad in (
        "http://online-edu.mirea.ru/course/view.php?id=1",
        "https://evil.example/course/view.php?id=1",
        "https://user:pw@online-edu.mirea.ru/course/view.php?id=1",
        "https://online-edu.mirea.ru:8443/course/view.php?id=1",
        "https://online-edu.mirea.ru.evil.example/course/view.php?id=1",
        f"{H}/mod/quiz/view.php?id=1",
        f"{H}/mod/quiz/attempt.php?attempt=1",
        f"{H}/mod/assign/view.php?id=1&action=editsubmission",
        f"{H}/course/view.php?id=1&sesskey=abc",
        f"{H}/mod/attendance/view.php?id=1",
        f"{H}/pluginfile.php/1/assignsubmission_file/submission_files/1/x.pdf",
        f"{H}/pluginfile.php/1/assignfeedback_file/feedback_files/1/x.pdf",
        f"{H}/pluginfile.php/1/user/private/0/x.pdf",
        f"{H}/pluginfile.php/1/mod_resource/content/0/..%2F..%2Fx.pdf",
        f"{H}/pluginfile.php/1/mod_resource/content/0/../x.pdf",
        f"{H}/login/index.php",
    ):
        assert safe_sdo_url(bad) is None, bad


def test_redact_url_strips_secrets():
    r = redact_url("https://u:p@external.example/doc?token=abc&wstoken=1&sesskey=2&x=1#frag")
    assert r == "https://external.example/doc?x=1"


def test_safe_component_names():
    assert safe_component("CON.pdf") == "_CON.pdf"
    assert safe_component("lpt1") == "_lpt1"
    assert safe_component("../../x") == "_.._x"
    assert safe_component("..") == "file"
    assert safe_component("Отчёт:1?.pdf") == "Отчёт_1_.pdf"
    assert safe_component("a\x00b\nc") == "a_b_c"
    long = safe_component("я" * 300 + ".pdf")
    assert len(long) <= 100 and long.endswith(".pdf")


# --- экспорт ---


async def test_export_all_courses_files_folders_and_exclusions():
    net = Net()
    async with net.client() as client:
        res = await export_materials(client, [CourseRef("1", "Матан"), CourseRef("2", "Физика/Лаб")], scope="all")
    files = _zip(res)
    m = res.manifest
    items = _items(m)

    # что скачано
    assert files["1 Матан/Лекции/_CON.pdf"].startswith(b"%PDF")
    assert "1 Матан/Лекции/Папка/_CON.pdf" in files
    assert files["1 Матан/Лекции/Папка/под/notes.txt"] == b"notes"
    assert "1 Матан/Лекции/Задание/task.pdf" in files
    assert "2 Физика_Лаб/Тема 1/Отчёт_1.pdf" in files and "2 Физика_Лаб/Тема 1/Отчёт_1 (2).pdf" in files
    page_md = files["1 Матан/Лекции/Страница.md"].decode()
    assert "Текст страницы" in page_md and "alert" not in page_md and "<script" not in page_md
    assign_md = files["1 Матан/Лекции/Задание/Задание.md"].decode()
    assert "Сделайте отчёт" in assign_md and "Отправлено" not in assign_md
    assert "Объявление для группы" in files["1 Матан/README.md"].decode()

    # статусы
    assert items["11"]["status"] == "saved" and items["12"]["status"] == "saved"
    assert len(items["12"]["files"]) == 2  # ссылка на чужую отправку отфильтрована
    assert items["14"] == {**items["14"], "status": "external_link", "external_url": "https://external.example/doc?x=1"}
    assert items["15"]["status"] == "skipped_interactive" and items["18"]["status"] == "skipped_interactive"
    assert items["19"]["status"] == "unsupported"
    assert items["17"]["status"] == "inline"
    assert res.status == "partial" and m["status"] == "partial"  # unknown module was not exported

    # не ходили никуда лишнего
    seen = net.paths()
    assert all(u.startswith(H + "/") for u in seen)
    for forbidden in ("quiz", "attendance", "mystery", "assignsubmission", "download_folder", "logout", "external.example"):
        assert not any(forbidden in u for u in seen), forbidden

    # в архиве нет cookie и sesskey
    blob = b"".join(files.values())
    assert COOKIE.encode() not in blob and SESSKEY.encode() not in blob
    assert all(not n.startswith("/") and ".." not in n.split("/") for n in files)


async def test_cross_origin_redirect_is_not_followed():
    routes = default_routes()
    routes["/course/view.php?id=2"] = lambda: _html(COURSE2)
    routes["/mod/resource/view.php?id=21"] = lambda: _redirect("https://evil.example/steal")
    routes["/mod/resource/view.php?id=22"] = lambda: _redirect(f"{H}/mod/quiz/attempt.php?attempt=1")
    net = Net(routes)
    async with net.client() as client:
        res = await export_materials(client, [CourseRef("2", "Физика")], scope="course:2")
    items = _items(res.manifest, "2")
    assert items["21"]["status"] == "failed" and items["22"]["status"] == "failed"
    assert res.status == "partial"
    assert not any("evil.example" in u or "quiz" in u for u in net.paths())


async def test_login_redirect_aborts_as_relogin_required():
    routes = default_routes()
    routes["/course/view.php?id=1"] = lambda: _redirect("https://sso.mirea.ru/realms/x/auth")
    net = Net(routes)
    async with net.client() as client:
        res = await export_materials(client, [CourseRef("1", "А"), CourseRef("2", "Б")], scope="all")
    assert res.status == "relogin_required"
    assert [c["status"] for c in res.manifest["courses"]] == ["failed", "not_started"]
    assert not any("sso.mirea.ru" in u for u in net.paths())


async def test_login_html_is_not_saved_as_pdf():
    routes = default_routes()
    routes["/mod/resource/view.php?id=21"] = lambda: httpx.Response(
        200, content=LOGIN_HTML, headers={"content-type": "application/pdf"}
    )
    routes["/mod/resource/view.php?id=22"] = lambda: _html("<html><body><p>нет ссылки</p></body></html>")
    net = Net(routes)
    async with net.client() as client:
        res = await export_materials(client, [CourseRef("2", "Б")], scope="course:2")
    files = _zip(res)
    assert not any(n.endswith(".pdf") for n in files)
    assert res.status == "relogin_required"  # тело — форма входа, даже с content-type pdf
    assert b"logintoken" not in b"".join(files.values())


async def test_size_limits_are_reported_not_truncated():
    routes = default_routes()
    routes["/mod/resource/view.php?id=21"] = lambda: _pdf("big.pdf", b"x" * 5000)
    routes["/mod/resource/view.php?id=22"] = lambda: _pdf("ok.pdf", b"y" * 100)
    net = Net(routes)
    limits = ExportLimits(max_file_bytes=1000)
    async with net.client() as client:
        res = await export_materials(client, [CourseRef("2", "Б")], scope="course:2", limits=limits)
    items = _items(res.manifest, "2")
    assert items["21"]["status"] == "too_large" and "path" not in items["21"]
    assert items["22"]["status"] == "saved"
    assert res.status == "partial"
    assert not any("big.pdf" in n for n in _zip(res))

    limits = ExportLimits(max_total_bytes=150)
    routes["/mod/resource/view.php?id=21"] = lambda: _pdf("a.pdf", b"x" * 100)
    async with Net(routes).client() as client:
        res = await export_materials(client, [CourseRef("2", "Б")], scope="course:2", limits=limits)
    items = _items(res.manifest, "2")
    assert items["21"]["status"] == "saved" and items["22"]["status"] == "limit_reached"
    assert res.status == "partial"


async def test_failed_or_empty_course_is_not_success():
    routes = default_routes()
    routes["/course/view.php?id=1"] = lambda: httpx.Response(500)
    routes["/course/view.php?id=2"] = lambda: _html("<html><body>пусто</body></html>")
    async with Net(routes).client() as client:
        res = await export_materials(client, [CourseRef("1", "А"), CourseRef("2", "Б")], scope="all")
    assert res.status == "failed"
    errs = {c["id"]: c["error"] for c in res.manifest["courses"]}
    assert "HTTP 500" in errs["1"] and "не найдено" in errs["2"]
    assert "manifest.json" in _zip(res)


async def test_partial_error_reporting_in_folder():
    routes = default_routes()
    del routes["/pluginfile.php/101/mod_folder/content/0/%D0%BF%D0%BE%D0%B4/notes.txt"]
    async with Net(routes).client() as client:
        res = await export_materials(client, [CourseRef("1", "А")], scope="course:1")
    folder = _items(res.manifest)["12"]
    assert folder["status"] == "partial"
    assert [f["status"] for f in folder["files"]] == ["saved", "failed"]
    assert "HTTP 404" in folder["files"][1]["error"]
    assert res.status == "partial"
    assert "[failed]" in _zip(res)["1 А/README.md"].decode()


# Export UI intentionally has no dashboard: CLI/MCP disk contracts are covered
# in test_material_commands.py and test_export_streaming.py.

