"""Observed portal boundaries: native session name + Moodle form_post. Synthetic."""

import httpx
import pytest
from pymirea import _settings
from pymirea.config import Config

from mirea_connect import login_session, services


@pytest.fixture
def config():
    previous = _settings._cfg
    _settings.configure(Config(session_keys="synthetic-only"))
    try:
        yield
    finally:
        _settings._cfg = previous


async def test_pulse_gate_uses_authenticated_read_not_legacy_cookie_name(config, monkeypatch):
    api = services.MireaGrades(session_cookies={"NativeSession": "SYNTHETIC-NATIVE"})
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    reads = []

    async def authenticated_logs():
        reads.append(True)
        return ["00000000-0000-0000-0000-000000000001"], None

    monkeypatch.setattr(api, "_get_visiting_logs", authenticated_logs)
    try:
        ok, _ = await api._ensure_aspnet_cookie()
        assert ok is True
        assert reads == [True]
    finally:
        await api.close()


async def test_pulse_gate_does_not_trust_cookie_presence_alone(config, monkeypatch):
    api = services.MireaGrades(session_cookies={".AspNetCore.Cookies": "SYNTHETIC-REJECTED"})

    async def rejected():
        return [], "unauthorized"

    monkeypatch.setattr(api, "_get_visiting_logs", rejected)
    try:
        ok, _ = await api._ensure_aspnet_cookie()
        assert ok is False
    finally:
        await api.close()


@pytest.mark.parametrize("mismatched_state", [False, True])
async def test_sdo_bootstrap_submits_only_exact_moodle_callback_and_reads_courses(config, mismatched_state):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path))
        if request.url.host == "online-edu.mirea.ru" and request.method == "GET" and request.url.path == "/auth/oidc/":
            return httpx.Response(303, headers={"location": "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth?state=PRIVATE-STATE"})
        if request.url.host == "sso.mirea.ru":
            body = '<form method="POST" action="https://online-edu.mirea.ru/auth/oidc/"><input type="hidden" name="code" value="PRIVATE-CODE"><input type="hidden" name="state" value="PRIVATE-STATE"><input type="hidden" name="iss" value="https://sso.mirea.ru/realms/mirea"><input type="submit" name="continue" value="Continue"></form>'
            return httpx.Response(200, text=body.replace('value="PRIVATE-STATE"', 'value="OTHER"') if mismatched_state else body)
        if request.url.host == "online-edu.mirea.ru" and request.method == "POST" and request.url.path == "/auth/oidc/":
            return httpx.Response(303, headers={"location": "/my/", "set-cookie": "MoodleSession=PRIVATE-MOODLE; Path=/; Secure; HttpOnly"})
        if request.url.path == "/my/":
            return httpx.Response(200, text='<script>M.cfg={"sesskey":"synthetic123"}</script>')
        if request.url.path == "/lib/ajax/service.php":
            return httpx.Response(200, json=[{"data": {"courses": [{"id": 7, "fullname": "Synthetic course"}]}}])
        raise AssertionError("Unexpected request")

    cookies = {"KEYCLOAK_IDENTITY": "PRIVATE-SSO"}
    api = login_session.MireaSDO(session_cookies=cookies)
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        result = await api.get_courses()
        if mismatched_state:
            assert result.success is False
            assert "__sdo_moodle_session" not in cookies
            assert not any(method == "POST" for method, _, _ in seen)
            return
        assert result.success, result.message
        assert len(result.courses) == 1
        assert cookies["__sdo_moodle_session"] == "PRIVATE-MOODLE"
        assert [r for r in seen if r[0] == "POST"] == [("POST", "online-edu.mirea.ru", "/auth/oidc/"), ("POST", "online-edu.mirea.ru", "/lib/ajax/service.php")]
    finally:
        await api.close()


@pytest.mark.parametrize("target", ["https://external.example/steal", "https://online-edu.mirea.ru/not-callback", "http://online-edu.mirea.ru/auth/oidc/"])
async def test_sdo_never_submits_callback_to_other_target(config, target):
    seen = []

    def handler(request):
        seen.append(request.method)
        if request.url.host == "online-edu.mirea.ru":
            return httpx.Response(303, headers={"location": "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth"})
        return httpx.Response(200, text='<form method="post" action="' + target + '"><input type="hidden" name="code" value="x"><input type="hidden" name="state" value="y"></form>')

    api = login_session.MireaSDO(session_cookies={"KEYCLOAK_IDENTITY": "synthetic"})
    await api.client.aclose()
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        assert await api.bootstrap_session() is False
        assert "POST" not in seen
    finally:
        await api.close()
