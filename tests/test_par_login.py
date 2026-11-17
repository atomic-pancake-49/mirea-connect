"""Regression for Pulse requiring PAR. Entire HTTP flow here is synthetic."""
import httpx
from pymirea import _settings
from pymirea.config import Config

from mirea_connect.local import MireaAuth


async def test_login_uses_pulse_par_then_preserves_otp_and_callback_cookies(monkeypatch):
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    seen = []

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path))
        path = request.url.path
        if request.url.host == "pulse.mirea.ru" and path == "/api/auth/login":
            return httpx.Response(302, headers={"location": "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth?client_id=attendance-app&request_uri=urn:test:par", "set-cookie": "Correlation=synthetic; Path=/; Secure"})
        if path == "/realms/mirea/protocol/openid-connect/auth":
            if "request_uri" not in request.url.params:
                return httpx.Response(302, headers={"location": "https://pulse.mirea.ru/?error=invalid_request&error_description=Pushed+Authorization+Request+is+only+allowed."})
            return httpx.Response(200, text='<script>{"loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/authenticate"}</script>')
        if path == "/realms/mirea/login-actions/authenticate":
            assert request.method == "POST"
            return httpx.Response(200, text='<script>{"pageId":"email-code-form","loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/otp"}</script>')
        if path == "/realms/mirea/login-actions/otp":
            from urllib.parse import parse_qs

            assert request.method == "POST"
            fields = parse_qs(request.content.decode())
            if fields.get("login") != ["true"]:
                return httpx.Response(200, text='<script>{"pageId":"email-code-form","loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/otp"}</script>')
            assert fields.get("emailCode") == ["synthetic-code"]
            return httpx.Response(302, headers={"location": "https://pulse.mirea.ru/signin-oidc?code=synthetic&state=synthetic"})
        if path == "/signin-oidc":
            assert "Correlation=synthetic" in request.headers.get("cookie", "")
            return httpx.Response(302, headers={"location": "/api/baseinfo", "set-cookie": ".AspNetCore.Cookies=synthetic-session; Path=/; Secure"})
        if path == "/api/baseinfo":
            return httpx.Response(200, json={"synthetic": True})
        return httpx.Response(200, text="<html><title>Pulse SPA</title></html>")

    auth = MireaAuth()
    await auth.client.aclose()
    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        result = await auth.login("synthetic@example.test", "not-a-real-password")
        assert result.challenge is not None, result.message
        result = await auth.complete_2fa(result.challenge, "synthetic-code")
        assert result.success, result.message
        assert result.cookies[".AspNetCore.Cookies"] == "synthetic-session"
        assert seen[0] == ("GET", "pulse.mirea.ru", "/api/auth/login")
        assert not any(path.endswith("/token") for _, _, path in seen)
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_untrusted_login_action_never_receives_password():
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text='<script>{"loginAction":"https://external.example/steal"}</script>')

    auth = MireaAuth()
    await auth.client.aclose()
    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await auth.login("synthetic@example.test", "not-a-real-password")
        assert not result.success
        assert all(request.method == "GET" and request.url.host == "pulse.mirea.ru" for request in requests)
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_extra_required_action_is_not_skipped_automatically():
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    try:
        response = httpx.Response(200, text='<script>{"pageId":"required-action"}</script>', request=httpx.Request("GET", "https://sso.mirea.ru/realms/mirea/login-actions/required-action"))
        result = await auth._result(response)
        assert not result.success and "дополнительного действия" in result.message
    finally:
        await auth.close()
        _settings._cfg = previous

