"""Independent exporter regression checks, synthetic HTTP only."""
import io
import zipfile

import httpx

from mirea_connect.materials import CourseRef, export_materials, pluginfile_links


def test_navigation_links_are_not_file_links():
    html = '''<a href="/course/view.php?id=1">Course</a>
    <a href="/mod/resource/view.php?id=2">Resource</a>
    <a href="/pluginfile.php/12/mod_folder/content/0/a.pdf">a.pdf</a>'''
    assert pluginfile_links(html, "https://online-edu.mirea.ru/mod/folder/view.php?id=3", {("mod_folder", "content")}) == [
        ("https://online-edu.mirea.ru/pluginfile.php/12/mod_folder/content/0/a.pdf", "a.pdf")
    ]


async def test_unknown_material_type_never_reports_complete():
    html = '<li class="activity modtype_mystery" id="module-1"><a href="/mod/mystery/view.php?id=1">Unknown</a></li>'
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef("1", "Synthetic")], scope="all")
    assert result.status == "partial"
    assert len(requests) == 1
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        assert "manifest.json" in archive.namelist()
