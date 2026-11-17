"""Independent review regressions, no live secrets or external network."""
import io
import json
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from bs4 import BeautifulSoup

from mirea_connect import cli, local, material_commands, services
from mirea_connect.auth import MireaAuth
from mirea_connect.materials import CourseRef, export_materials, redact_url
from mirea_connect.session_state import LocalSessionStore


@pytest.mark.parametrize('mode', ['cli', 'mcp'])
async def test_failed_export_result_cannot_promote_session(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('PYMIREA_SESSION_KEYS', 'synthetic-review-key')
    store = LocalSessionStore(tmp_path / 'session')
    store.configure()
    original = {'Pulse.Auth.Cookie': 'SYNTHETIC-ORIGINAL'}
    store.save(original)
    candidate = store.save_candidate({'Pulse.Auth.Cookie': 'SYNTHETIC-CANDIDATE'})

    async def failed(cookies, course=None):
        cookies['Pulse.Auth.Cookie'] = 'SYNTHETIC-REJECTED'
        return {'status': 'relogin_required'}

    monkeypatch.setattr(material_commands, 'export_to_disk', failed)
    if mode == 'mcp':
        server, _ = local.build_local(store)
        await server.call_tool('export_materials', {})
    else:
        await material_commands.export_for_store(store)
    assert LocalSessionStore(store.home).load().cookies == original
    assert LocalSessionStore(store.home).load_candidate().candidate_id == candidate


async def test_partial_sdo_does_not_promote_changed_state(tmp_path, monkeypatch):
    monkeypatch.setenv('PYMIREA_SESSION_KEYS', 'synthetic-review-key')
    store = LocalSessionStore(tmp_path / 'session')
    store.configure()
    original = {'Pulse.Auth.Cookie': 'SYNTHETIC-ORIGINAL'}
    store.save(original)

    class Partial:
        def __init__(self, session_cookies):
            self.cookies = session_cookies

        async def get_courses(self):
            self.cookies['Pulse.Auth.Cookie'] = 'SYNTHETIC-REJECTED'
            return SimpleNamespace(success=False, courses=[], message='unauthorized')

        async def get_deadlines(self, **kw):
            return SimpleNamespace(success=True, deadlines=[])

        async def get_grades(self, **kw):
            return SimpleNamespace(success=True, grades=[])

        async def close(self):
            pass

    monkeypatch.setattr(services, 'MireaSDO', Partial)
    async with local.LocalSource(store).use() as cookies:
        await services.sdo_overview(cookies)
    assert LocalSessionStore(store.home).load().cookies == original


@pytest.mark.parametrize('phase', ['listing', 'close'])
def test_materials_cli_masks_listing_and_close_exceptions(tmp_path, monkeypatch, capsys, phase):
    class Store:
        def load(self):
            return SimpleNamespace(cookies={'Pulse.Auth.Cookie': 'SYNTHETIC'})

    class Failing:
        def __init__(self, **kwargs):
            pass

        async def get_courses(self):
            if phase == 'listing':
                raise RuntimeError('SYNTHETIC-CLOSE-SECRET')
            return SimpleNamespace(success=True, courses=[])

        async def close(self):
            if phase == 'close':
                raise RuntimeError('SYNTHETIC-CLOSE-SECRET')

    monkeypatch.setattr(cli, '_store', lambda: Store())
    monkeypatch.setattr(material_commands, 'MireaSDO', Failing)
    assert cli.main(['materials', '--all', '--json']) == 1
    err = capsys.readouterr().err
    assert 'SYNTHETIC-CLOSE-SECRET' not in err and 'Traceback' not in err


async def test_sso_error_text_is_connector_owned():
    auth = MireaAuth()
    try:
        response = httpx.Response(200, text='<script>kcContext={"message":"sesskey=SYNTHETIC-SSO-SECRET","pageId":"login"}</script>', request=httpx.Request('GET', 'https://sso.mirea.ru/realms/mirea/login-actions/authenticate'))
        result = await auth._result(response)
        assert 'SYNTHETIC-SSO-SECRET' not in result.message
    finally:
        await auth.close()


def test_login_close_error_is_masked(tmp_path, monkeypatch, capsys):
    from mirea_connect import auth

    class FakeAuth:
        diagnostics = []
        checkpoint_error = False

        async def login(self, *args):
            return SimpleNamespace(challenge=None, cookies=None, message='Вход не подтверждён')

        async def close(self):
            raise RuntimeError('SYNTHETIC-AUTH-CLOSE-SECRET')

    monkeypatch.setenv('PYMIREA_SESSION_KEYS', 'synthetic-review-key')
    store = LocalSessionStore(tmp_path / 'session')
    monkeypatch.setattr(cli, '_store', lambda: store)
    monkeypatch.setattr(auth, 'MireaAuth', FakeAuth)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda *args: 'synthetic-not-a-password')
    assert cli.main(['login', 'synthetic@example.test']) == 1
    output = capsys.readouterr()
    assert 'SYNTHETIC-AUTH-CLOSE-SECRET' not in output.err + output.out
    assert 'Traceback' not in output.err


async def test_nested_url_secrets_removed_but_other_query_semantics_preserved():
    course = '<main id="region-main"><li class="activity modtype_page" id="module-1"><a href="/mod/page/view.php?id=1">Page</a></li></main>'
    page = '<main id="region-main"><a href="https://example.test/?next=sesskey%3DSYNTHETIC-NESTED-SECRET">Link</a><a href="https://example.test/?q=a%26b">Safe</a></main>'

    def handle(request):
        assert request.url.host == 'online-edu.mirea.ru'
        return httpx.Response(200, text=course if request.url.path == '/course/view.php' else page, headers={'content-type': 'text/html'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await export_materials(client, [CourseRef('1', 'A')], scope='all')
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode()
            assert 'SYNTHETIC-NESTED-SECRET' not in unquote(text)
            if name.endswith('.md'):
                assert 'https://example.test/?q=a%26b' in text or '/README.md' in name
    assert parse_qs(urlsplit(redact_url('https://example.test/?q=a%26b')).query) == {'q': ['a&b']}


async def test_redacted_names_keep_manifest_paths_resolvable():
    course = '<main id="region-main"><li class="activity modtype_resource" id="module-1"><a href="/mod/resource/view.php?id=1">token=SYNTHETIC-NAME-SECRET</a></li></main>'

    def handle(request):
        if request.url.path == '/course/view.php':
            return httpx.Response(200, text=course, headers={'content-type': 'text/html'})
        return httpx.Response(200, content=b'file', headers={'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="file.pdf"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await export_materials(client, [CourseRef('1', 'token=SYNTHETIC-NAME-SECRET')], scope='all')
    assert result.status == 'complete'
    assert 'SYNTHETIC-NAME-SECRET' not in json.dumps(result.manifest)
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        for course in result.manifest['courses']:
            assert course['page_snapshot']['path'] in archive.namelist()
            for item in course['items']:
                assert item['path'] in archive.namelist()
        for name in archive.namelist():
            if name.endswith('.html'):
                for tag in BeautifulSoup(archive.read(name), 'html.parser').select('a[href]'):
                    assert 'SYNTHETIC-NAME-SECRET' not in tag['href']


@pytest.mark.parametrize('book_id', ['', 'abc', '0', '42'])
async def test_library_id_validation_preserves_optional_metadata(book_id):
    course = '<main id="region-main"><li class="activity modtype_lanebs" id="module-1"><a href="/mod/lanebs/view.php?id=1">Book</a></li></main>'

    def handle(request):
        return httpx.Response(200, text=course if request.url.path == '/course/view.php' else f'<main id="region-main"><div class="item" data-id="{book_id}" data-type="book"></div></main>', headers={'content-type': 'text/html'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await export_materials(client, [CourseRef('1', 'A')], scope='all')
    assert result.status == ('complete' if book_id == '42' else 'partial')
    assert result.manifest['totals']['metadata_only'] == (1 if book_id == '42' else 0)
