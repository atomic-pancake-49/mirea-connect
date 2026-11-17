"""Final Astra URL-path projection regression; requests stay unchanged."""
import io
import json
import posixpath
import zipfile
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from bs4 import BeautifulSoup

from mirea_connect.materials import CourseRef, _scrub_data, export_materials, redact_url, safe_component


@pytest.mark.parametrize('assignment', ['token%3DSYNTHETIC-PATH-SECRET.pdf', 'token%253DSYNTHETIC-PATH-SECRET.pdf', 'sesskey=SYNTHETIC-PATH-SECRET.pdf'])
def test_secret_filename_url_projection(assignment):
    url = 'https://online-edu.mirea.ru/pluginfile.php/1/mod_resource/content/0/' + assignment
    path = '1 A/' + safe_component('token=SYNTHETIC-PATH-SECRET.pdf')
    projected = _scrub_data({'path': path, 'file_url': url})
    assert projected['path'] == path
    assert 'SYNTHETIC-PATH-SECRET' not in unquote(unquote(json.dumps(projected)))
    assert redact_url('https://example.test/a%20b.pdf?q=a%26b') == 'https://example.test/a%20b.pdf?q=a%26b'


async def test_secret_file_url_not_in_generated_archive_and_local_links_resolve():
    remote = 'https://online-edu.mirea.ru/pluginfile.php/1/mod_resource/content/0/token%3DSYNTHETIC-PATH-SECRET.pdf'
    course = '<main id="region-main"><li class="activity modtype_resource" id="module-1"><a href="/mod/resource/view.php?id=1">File</a></li></main>'
    landing = '<main id="region-main"><a href="' + remote + '">File</a></main>'
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if request.url.path == '/course/view.php':
            return httpx.Response(200, text=course, headers={'content-type': 'text/html'})
        if request.url.path == '/mod/resource/view.php':
            return httpx.Response(200, text=landing, headers={'content-type': 'text/html'})
        assert str(request.url) == remote
        return httpx.Response(200, content=b'synthetic PDF bytes', headers={'content-type': 'application/pdf'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await export_materials(client, [CourseRef('1', 'A')], scope='course:1')
    assert result.status == 'complete'
    assert remote in seen  # Redaction affects output, never the authenticated request.
    assert 'SYNTHETIC-PATH-SECRET' not in unquote(json.dumps(result.manifest))
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        names = set(archive.namelist())
        item = result.manifest['courses'][0]['items'][0]
        assert item['path'] in names and item['page_snapshot']['path'] in names
        for name in names:
            assert 'SYNTHETIC-PATH-SECRET' not in unquote(name)
            if not name.endswith(('.html', '.md', '.json')):
                continue
            text = archive.read(name).decode()
            assert 'SYNTHETIC-PATH-SECRET' not in unquote(unquote(text))
            if name.endswith('.html'):
                for tag in BeautifulSoup(text, 'html.parser').select('a[href]'):
                    target = urlsplit(tag['href'])
                    if not target.scheme and not target.netloc and target.path:
                        assert posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(target.path))) in names
