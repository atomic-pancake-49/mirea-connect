"""Recovery acceptance: no auth prompts, no real network or deletion."""
import json

import pytest
from pymirea import _settings, tokens
from pymirea.exceptions import MireaSessionExpired

from mirea_connect import auth, cli, commands, local, login_session, services
from mirea_connect.session_state import LocalSessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    previous = _settings._cfg
    _settings._cfg = None
    monkeypatch.setenv("PYMIREA_SESSION_KEYS", "synthetic-recovery-key")
    value = LocalSessionStore(tmp_path / "home")
    value.configure()
    try:
        yield value
    finally:
        _settings._cfg = previous


async def failed_read(cookies):
    raise RuntimeError("PRIVATE-ERROR-AND-URL-MUST-NOT-LEAK")


async def pulse_ok(cookies):
    assert cookies.get(".AspNetCore.Cookies")
    return {"subjects": [{"name": "synthetic"}]}


async def sdo_ok(cookies):
    cookies["__sdo_moodle_session"] = "PRIVATE-MOODLE-COOKIE"
    return {"courses_count": 2}


async def test_failed_verification_preserves_old_active_and_candidate(store):
    store.save({".AspNetCore.Cookies": "PRIVATE-OLD"})
    ident = store.save_candidate({".AspNetCore.Cookies": "PRIVATE-NEW"})
    before = {p.name: p.read_bytes() for p in store.records_path.iterdir()}
    result = await login_session.verify_candidate(store, ident, pulse_reader=failed_read, sdo_reader=failed_read)
    assert result["status"] == "candidate"
    assert store.load().cookies[".AspNetCore.Cookies"] == "PRIVATE-OLD"
    assert store.load_candidate(ident).cookies[".AspNetCore.Cookies"] == "PRIVATE-NEW"
    assert before == {p.name: p.read_bytes() for p in store.records_path.iterdir()}
    assert "PRIVATE-ERROR" not in json.dumps(result)


async def test_failed_probe_cannot_replace_default_candidate_with_mutated_cookie(store):
    ident = store.save_candidate({".AspNetCore.Cookies": "PRIVATE-ORIGINAL"})

    async def failed_and_mutated(cookies):
        cookies[".AspNetCore.Cookies"] = "PRIVATE-REJECTED"
        raise RuntimeError("synthetic failure after cookie mutation")

    result = await login_session.verify_candidate(store, ident, pulse_reader=failed_and_mutated, sdo_reader=failed_read)
    assert result["status"] == "candidate"
    assert result["candidate_id"] == ident
    assert store.load_candidate().candidate_id == ident
    assert store.load_candidate().cookies[".AspNetCore.Cookies"] == "PRIVATE-ORIGINAL"


async def test_pulse_verified_is_separate_from_unverified_sdo(store):
    ident = store.save_candidate({".AspNetCore.Cookies": "PRIVATE-NEW"})
    result = await login_session.verify_candidate(store, ident, pulse_reader=pulse_ok, sdo_reader=failed_read)
    assert result["status"] == "verified"
    assert result["pulse"]["status"] == "verified"
    assert result["sdo"]["status"] == "unverified"
    assert store.load().cookies[".AspNetCore.Cookies"] == "PRIVATE-NEW"


async def test_chunked_cookie_and_moodle_cache_survive_new_store_process_boundary(store):
    cookies = {".AspNetCore.Cookies": "chunks-2", ".AspNetCore.CookiesC1": "PRIVATE-CHUNK1", ".AspNetCore.CookiesC2": "PRIVATE-CHUNK2"}
    ident = store.save_candidate(cookies)
    seen = []

    async def read(c):
        seen.append(dict(c))
        assert all(c[k] == v for k, v in cookies.items())
        return {"subjects": []}

    answer = await login_session.verify_candidate(store, ident, pulse_reader=read, sdo_reader=sdo_ok)
    assert answer["status"] == "verified" and seen
    restored = LocalSessionStore(store.home).load().cookies
    assert all(restored[k] == v for k, v in cookies.items())
    assert restored["__sdo_moodle_session"] == "PRIVATE-MOODLE-COOKIE"
    assert "PRIVATE-" not in json.dumps(answer)


async def test_cli_recovery_and_mcp_share_operation_without_auth_or_getpass(store, monkeypatch, capsys):
    ident = store.save_candidate({".AspNetCore.Cookies": "PRIVATE-COOKIE"})

    def forbidden(*args, **kwargs):
        raise AssertionError("Recovery must not authenticate or prompt")

    monkeypatch.setattr(auth, "MireaAuth", forbidden)
    monkeypatch.setattr(local, "MireaAuth", forbidden)
    monkeypatch.setattr(cli.getpass, "getpass", forbidden)
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(services, "grades", pulse_ok)
    monkeypatch.setattr(login_session, "_pulse_read", pulse_ok)
    monkeypatch.setattr(login_session, "_sdo_read", sdo_ok)
    assert await cli._verify_login_command(ident, use_json=True) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["status"] == "verified"
    mcp, _ = local.build_local(LocalSessionStore(store.home))
    result = await mcp.call_tool("verify_login", {"candidate_id": ident})
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert json.loads(text)["status"] == "verified"
    assert "PRIVATE-COOKIE" not in text


async def test_cli_expiry_never_clears(store, monkeypatch):
    store.save({"access_token": "synthetic-expired"})

    async def expired(*args, **kwargs):
        raise MireaSessionExpired("synthetic")

    monkeypatch.setattr(tokens, "ensure_fresh_token", expired)
    with pytest.raises(services.SessionExpired):
        async with commands._session(store):
            raise AssertionError("expired session must not yield")
    assert store.exists()


async def test_local_expiry_never_clears_even_with_legacy_true_flag(store, monkeypatch):
    store.save({"access_token": "synthetic-expired"})

    async def expired(*args, **kwargs):
        raise MireaSessionExpired("synthetic")

    monkeypatch.setattr(local, "ensure_fresh_token", expired)
    with pytest.raises(local.NoSession):
        async with local.LocalSource(store, clear_on_expiry=True).use():
            raise AssertionError("expired session must not yield")
    assert store.exists()


def test_diagnostic_projection_never_keeps_arbitrary_secret_values():
    rows = [{"phase": "callback", "host": "pulse.mirea.ru", "path": "/secret/PRIVATE-URL?token=PRIVATE-QUERY", "status": 404,
             "message": "PRIVATE-MESSAGE", "authorization": "PRIVATE-HEADER", "raw_html": "PRIVATE-HTML",
             "submitted_fields": ["code", "state", "PRIVATE-FIELD"],
             "cookies": [{"class": "PRIVATE-NAME", "name": "PRIVATE-NAME", "value": "PRIVATE-COOKIE", "path": "/PRIVATE-PATH", "domain": "PRIVATE-DOMAIN", "secure": True}],
             "forms": [{"method": "post", "action": "pulse_callback", "known_fields": ["code", "PRIVATE-FIELD"], "unknown_field_count": 1}]}]
    projected = login_session.safe_diagnostics(rows)
    assert "PRIVATE-" not in json.dumps(projected)
    assert projected[0]["path"] == "other"
    assert projected[0]["submitted_fields"] == ["code", "state"]
