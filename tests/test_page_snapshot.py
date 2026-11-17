"""Static HTML export contracts, synthetic pages and in-memory HTTP only."""
import io
import zipfile

import httpx
from bs4 import BeautifulSoup

from mirea_connect.materials import CourseRef, ExportLimits, export_materials
from mirea_connect.page_snapshot import static_page


def test_static_page_removes_active_and_session_data():
    html = '''<html><head><script>secret()</script></head><body>
    <div id="region-main" onclick="bad()" data-token="PRIVATE">
    <h2>Раздел</h2><details><summary>Тема</summary><p>Содержимое</p></details>
    <table><tr><td colspan="2">Формула</td></tr></table>
    <script>danger()</script><iframe src="https://evil.example"></iframe>
    <form><input name="password" value="PRIVATE"></form>
    <a href="javascript:bad()" onmouseover="bad()">Unsafe</a>
    <a href="https://online-edu.mirea.ru/course/view.php?id=1&sesskey=PRIVATE">Источник</a>
    <div class="submissionstatustable">PERSONAL-SUBMISSION</div>
    </div></body></html>'''
    body = static_page(html, "Курс <test>", "https://online-edu.mirea.ru/course/view.php?id=1")
    soup = BeautifulSoup(body, "html.parser")
    assert soup.find("h2").text == "Раздел"
    assert soup.find("details").has_attr("open")
    assert soup.find("td")["colspan"] == "2"
    assert not soup.find(["script", "form", "input", "iframe"])
    assert not soup.find(attrs={"onclick": True})
    assert b"PRIVATE" not in body and b"PERSONAL-SUBMISSION" not in body
    assert "javascript:" not in body.decode()
    assert "default-src 'none'" in soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"})["content"]


COURSE = '''<html><body><main id="region-main"><h2>Полный раздел курса</h2>
<p>Общее описание, не являющееся отдельным файлом.</p>
<li class="activity modtype_page" id="module-11"><a href="https://online-edu.mirea.ru/mod/page/view.php?id=11">Лекция</a></li>
<li class="activity modtype_assign" id="module-12"><a href="https://online-edu.mirea.ru/mod/assign/view.php?id=12">Задание</a></li>
<li class="activity modtype_quiz" id="module-13"><a href="https://online-edu.mirea.ru/mod/quiz/view.php?id=13">Тест</a></li>
</main></body></html>'''
PAGE = '''<main id="region-main"><h2>Текст всей страницы</h2><table><tr><td>Данные</td></tr></table>
<img src="/pluginfile.php/1/mod_page/content/0/picture.png" alt="Схема">
<img src="https://external.example/tracker.png" alt="Внешняя картинка">
<script>track()</script></main>'''
ASSIGN = '''<main id="region-main"><div id="intro"><h2>Условия</h2><p>Описание задания</p></div>
<div class="submissionstatustable">PERSONAL-SUBMISSION</div></main>'''


async def test_export_includes_course_and_activity_html_with_embedded_images():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/course/view.php":
            text = COURSE
        elif request.url.path == "/mod/page/view.php":
            text = PAGE
        elif request.url.path == "/mod/assign/view.php":
            text = ASSIGN
        elif request.url.path.startswith("/pluginfile.php/"):
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nsynthetic", headers={"content-type": "image/png"})
        else:
            raise AssertionError("unexpected request: " + str(request.url))
        return httpx.Response(200, text=text, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef("1", "Курс")], scope="all")
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        course = result.manifest["courses"][0]
        snapshot = course["page_snapshot"]
        course_html = archive.read(snapshot["path"]).decode()
        assert "Полный раздел курса" in course_html and "Общее описание" in course_html
        items = {item["cmid"]: item for item in course["items"]}
        page_snapshot = items["11"]["page_snapshot"]
        page_html = archive.read(page_snapshot["path"]).decode()
        assert "data:image/png;base64," in page_html
        assert "Текст всей страницы" in page_html and "<table>" in page_html
        assert "track()" not in page_html
        assert page_snapshot["images"] == 1
        assert page_snapshot["status"] == "partial" and page_snapshot["omissions"]
        assign_html = archive.read(items["12"]["page_snapshot"]["path"]).decode()
        assert "Описание задания" in assign_html and "PERSONAL-SUBMISSION" not in assign_html
        for item in (items["11"], items["12"]):
            relative = item["page_snapshot"]["path"].split("/", 1)[1]
            assert relative in course_html
    assert result.status == "partial"  # external image was deliberately not fetched
    assert not any("external.example" in url or "/mod/quiz/" in url for url in seen)


async def test_snapshot_size_limit_is_explicit():
    def handler(request):
        return httpx.Response(200, text=COURSE, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef("1", "Курс")], scope="all", limits=ExportLimits(max_total_bytes=10))
    assert result.status != "complete"
    assert result.manifest["courses"][0]["page_snapshot"]["status"] == "limit_reached"


async def test_folder_page_links_to_downloaded_file_offline():
    course_html = '<main id="region-main"><li class="activity modtype_folder" id="module-20"><a href="/mod/folder/view.php?id=20">Folder</a></li></main>'
    folder_html = '<main id="region-main"><h2>Files</h2><a href="/pluginfile.php/1/mod_folder/content/0/doc.pdf">doc.pdf</a></main>'

    def handler(request):
        if request.url.path.startswith("/pluginfile.php/"):
            return httpx.Response(200, content=b"synthetic-pdf", headers={"content-type": "application/pdf"})
        return httpx.Response(200, text=course_html if request.url.path == "/course/view.php" else folder_html, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef("1", "Course")], scope="all")
    assert result.status == "complete"
    item = result.manifest["courses"][0]["items"][0]
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        snapshot = archive.read(item["page_snapshot"]["path"]).decode()
        assert 'href="doc.pdf"' in snapshot
        assert item["files"][0]["path"] in archive.namelist()

