"""Library resources are metadata + links only; never fetch the reader."""
import io
import json
import zipfile

import httpx
from bs4 import BeautifulSoup

from mirea_connect.materials import CourseRef, export_materials, safe_sdo_url

H = "https://online-edu.mirea.ru"


async def test_six_library_cards_export_metadata_links_without_reader_requests():
    seen = []
    course = '<main id="region-main">' + ''.join(
        f'<li class="activity modtype_lanebs" id="module-{i}"><a href="/mod/lanebs/view.php?id={i}">Учебник {i}, стр. {i}</a></li>'
        for i in range(1, 7)
    ) + '</main>'

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path, request.url.params.get("id")))
        assert request.method == "GET" and request.url.host == "online-edu.mirea.ru"
        if request.url.path == "/course/view.php":
            body = course
        elif request.url.path == "/mod/lanebs/view.php":
            i = int(request.url.params["id"])
            body = f'''<main id="region-main"><div class="item-container">
              <img src="https://external.example/cover.png">
              <div class="item" data-id="{100 + i}" data-page="{i}" data-type="book">
                <p>Учебник {i}</p><div class="biblio_record">Автор {i}. Учебник. — 2026. — ISBN {i}.</div>
                <button data-action="book_modal">Читать</button>
              </div></div><script>PRIVATE-TOKEN</script>
              <iframe src="https://reader.lanbook.com/old/book/{100 + i}?token=PRIVATE-TOKEN"></iframe>
              </main>'''
        else:
            raise AssertionError("No file, AJAX, cover or reader request is allowed")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef("42", "Курс")], scope="course:42")
    assert result.status == "complete"
    items = result.manifest["courses"][0]["items"]
    assert len(items) == 6
    assert result.manifest["totals"]["metadata_only"] == 6
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        assert archive.testzip() is None
        course_html = archive.read(result.manifest["courses"][0]["page_snapshot"]["path"]).decode()
        for i, item in enumerate(items, 1):
            assert item["status"] == "metadata_only"
            assert item["fulltext_downloaded"] is False
            assert item["metadata"]["bibliography"] == f"Автор {i}. Учебник. — 2026. — ISBN {i}."
            assert item["metadata"]["page"] == i
            assert item["metadata"]["book_id"] == str(100 + i)
            assert item["external_url"] == f"https://e.lanbook.com/book/{100 + i}"
            assert item["path"].split("/", 1)[1] in course_html
            card = archive.read(item["path"]).decode()
            soup = BeautifulSoup(card, "html.parser")
            assert not soup.select("script,iframe,form,button,img")
            assert "Полный текст не скачивался" in soup.get_text()
            assert soup.find("a", href=item["external_url"])
            assert soup.find("a", href=item["url"])
            assert item["metadata"]["bibliography"] in soup.get_text()
            assert "PRIVATE-TOKEN" not in card
        assert "PRIVATE-TOKEN" not in json.dumps(result.manifest)
        assert any("полные тексты" in n.lower() and "lanebs" in n for n in result.manifest["notes"])
    assert len(seen) == 7


async def test_library_error_page_is_not_successful_metadata():
    course = '<main id="region-main"><li class="activity modtype_lanebs" id="module-1"><a href="/mod/lanebs/view.php?id=1">Book</a></li></main>'

    def handler(request):
        return httpx.Response(200, text=course if request.url.path == '/course/view.php' else '<main id="region-main">Доступ запрещён</main>', headers={'content-type': 'text/html'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef('1', 'Synthetic')], scope='course:1')
    assert result.status == 'partial'
    assert result.manifest['totals']['metadata_only'] == 0
    item = result.manifest['courses'][0]['items'][0]
    assert item['status'] == 'failed'
    assert 'path' not in item


def test_library_module_allowlist_is_metadata_page_only():
    assert safe_sdo_url(H + "/mod/lanebs/view.php?id=17") == H + "/mod/lanebs/view.php?id=17"
    for path in ("/mod/lanebs/view.php?id=17&action=read", "/mod/lanebs/view.php?id=17&sesskey=PRIVATE", "/mod/lanebs/reader.php?id=17"):
        assert safe_sdo_url(H + path) is None
    assert safe_sdo_url("https://reader.lanbook.com/old/book/17") is None
