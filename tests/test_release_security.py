"""Audit-confirmed P1 regressions. Synthetic state; no live account/network."""
import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from mirea_connect import commands, local, services
from mirea_connect.materials import CourseRef, export_materials
from mirea_connect.session_state import LocalSessionStore


def test_otp_browser_field_is_masked(tmp_path):
    from bs4 import BeautifulSoup
    page = local.LocalLogin(LocalSessionStore(tmp_path / 'synthetic'))
    code = BeautifulSoup(page._otp().body, 'html.parser').find('input', attrs={'name': 'code'})
    assert code.get('type') == 'password'
    assert code.get('autocomplete') == 'one-time-code'


@pytest.mark.parametrize('mode',['cli','mcp'])
async def test_failed_read_cannot_promote_mutated_cookies(tmp_path,monkeypatch,mode):
    monkeypatch.setenv('PYMIREA_SESSION_KEYS','synthetic-regression-key')
    store=LocalSessionStore(tmp_path/'session')
    store.configure()
    original={'Pulse.Auth.Cookie':'SYNTHETIC-ORIGINAL'}
    store.save(original)
    candidate=store.save_candidate({'Pulse.Auth.Cookie':'SYNTHETIC-CANDIDATE'})
    context=commands._session(store) if mode=='cli' else local.LocalSource(store).use()
    with pytest.raises(RuntimeError):
        async with context as cookies:
            cookies['Pulse.Auth.Cookie']='SYNTHETIC-REJECTED'
            cookies['__pulse_bootstrap_failed_at']=123
            raise RuntimeError('failed read')
    assert LocalSessionStore(store.home).load().cookies==original
    assert LocalSessionStore(store.home).load_candidate().candidate_id==candidate


@pytest.mark.parametrize('message',[
    'grpc-message sesskey=SYNTHETIC-LEAK-KEY',
    'Ошибка сети: request with token=SYNTHETIC-LEAK-KEY',
    'Сессия истекла; cookie=SYNTHETIC-LEAK-KEY',
])
async def test_public_mcp_error_does_not_echo_arbitrary_upstream_text(tmp_path,monkeypatch,message):
    monkeypatch.setenv('PYMIREA_SESSION_KEYS','synthetic-regression-key')
    store=LocalSessionStore(tmp_path/'session')
    store.configure()
    store.save({'Pulse.Auth.Cookie':'SYNTHETIC'})
    class Failed:
        def __init__(self,**kwargs):pass
        async def get_grades(self):return SimpleNamespace(success=False,message=message)
        async def close(self):pass
    monkeypatch.setattr(services,'MireaGrades',Failed)
    server,_=local.build_local(store)
    try:
        response=await server.call_tool('my_grades',{})
        text=''.join(getattr(c,'text','') for c in response.content)
    except Exception as error:
        text=str(error)
    assert 'SYNTHETIC-LEAK-KEY' not in text
    assert text


@pytest.mark.parametrize('entrypoint', ['cli', 'mcp'])
async def test_raised_upstream_exception_is_not_echoed(tmp_path, monkeypatch, capsys, entrypoint):
    from mirea_connect import cli

    monkeypatch.setenv('PYMIREA_SESSION_KEYS', 'synthetic-regression-key')
    store = LocalSessionStore(tmp_path / 'session')
    store.configure()
    store.save({'Pulse.Auth.Cookie': 'SYNTHETIC'})

    class Failed:
        def __init__(self, **kwargs):
            pass

        async def get_grades(self):
            raise RuntimeError('sesskey=SYNTHETIC-THROWN-KEY')

        async def close(self):
            pass

    monkeypatch.setattr(services, 'MireaGrades', Failed)
    if entrypoint == 'mcp':
        server, _ = local.build_local(store)
        try:
            response = await server.call_tool('my_grades', {})
            text = str(response)
        except Exception as error:
            text = str(error)
    else:
        # Invoke synchronous main in a separate thread, not inside its event loop.
        import asyncio
        monkeypatch.setattr(cli, '_store', lambda: store)
        try:
            result = await asyncio.to_thread(cli.main, ['grades', '--json'])
            assert result == 1
            text = capsys.readouterr().err
        except Exception as error:
            text = str(error)
    assert 'SYNTHETIC-THROWN-KEY' not in text
    assert text


@pytest.mark.parametrize('mode', ['cli', 'mcp'])
async def test_successful_read_can_retain_cache(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('PYMIREA_SESSION_KEYS', 'synthetic-regression-key')
    store = LocalSessionStore(tmp_path / 'session')
    store.configure()
    store.save({'Pulse.Auth.Cookie': 'SYNTHETIC'}, login='synthetic@example.test')
    context = commands._session(store) if mode == 'cli' else local.LocalSource(store).use()
    async with context as cookies:
        cookies['__sdo_moodle_session'] = 'SYNTHETIC-CACHE'
        from mirea_connect.session_state import approve_session_updates
        approve_session_updates(cookies)
    fresh = LocalSessionStore(store.home).load()
    assert fresh.cookies['__sdo_moodle_session'] == 'SYNTHETIC-CACHE'
    assert fresh.login == 'synthetic@example.test'


async def test_zip_cannot_leak_session_value_from_link_label():
    course='<main id="region-main"><li class="activity modtype_page" id="module-1"><a href="/mod/page/view.php?id=1">Page</a></li></main>'
    page='<main id="region-main"><a href="/course/view.php?id=1&sesskey=SYNTHETIC-LEAK-KEY">sesskey=SYNTHETIC-LEAK-KEY</a></main>'
    def handle(request):
        return httpx.Response(200,text=course if request.url.path=='/course/view.php' else page,headers={'content-type':'text/html'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await export_materials(client,[CourseRef('1','Synthetic')],scope='course:1')
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        assert all(b'SYNTHETIC-LEAK-KEY' not in archive.read(n) for n in archive.namelist())
