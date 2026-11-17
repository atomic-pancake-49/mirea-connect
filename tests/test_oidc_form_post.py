"""OIDC form_post is a callback handoff, not another MFA form. Synthetic only."""
import json
from urllib.parse import parse_qs

import httpx
import pytest
from pymirea import _settings
from pymirea.config import Config

from mirea_connect.auth import MireaAuth

CALLBACK = "https://pulse.mirea.ru/api/mireaauth"
SOURCE = "https://sso.mirea.ru/realms/mirea/login-actions/required-action"


def form_page(action=CALLBACK, *, method="post", fields=None):
    from html import escape
    fields = fields if fields is not None else [("code", "SYNTHETIC-PRIVATE-CODE"), ("state", "SYNTHETIC-PRIVATE-STATE"), ("session_state", "SYNTHETIC-PRIVATE-SESSION")]
    inputs = "".join('<input type="hidden" name="' + escape(k, quote=True) + '" value="' + escape(v, quote=True) + '">' for k, v in fields)
    return '<html><body onload="document.forms[0].submit()"><form method="' + method + '" action="' + escape(action, quote=True) + '">' + inputs + '</form></body></html>'


async def run_form(html, *, source=SOURCE, repeated=False):
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    await auth.client.aclose()
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "pulse.mirea.ru" and request.url.path == "/api/mireaauth":
            assert request.method == "POST"
            assert "Correlation=SYNTHETIC-CORRELATION" in request.headers.get("cookie", "")
            if repeated:
                return httpx.Response(302, headers={"location": SOURCE})
            return httpx.Response(302, headers={"location": "/", "set-cookie": ".AspNetCore.Cookies=SYNTHETIC-PULSE; Path=/; Secure"})
        if request.url.host == "sso.mirea.ru" and request.url.path.endswith("required-action"):
            return httpx.Response(200, text=html)
        if request.url.host == "pulse.mirea.ru" and request.url.path == "/":
            return httpx.Response(200, json={"synthetic": True})
        raise AssertionError("unexpected request")

    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth.client.cookies.set("Correlation", "SYNTHETIC-CORRELATION", domain="pulse.mirea.ru", path="/")
    try:
        response = httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=httpx.Request("POST", source, data={"skip": "true"}))
        result = await auth._result(response)
        diagnostics = json.dumps(auth.diagnostics)
        for secret in ("SYNTHETIC-PRIVATE-CODE", "SYNTHETIC-PRIVATE-STATE", "SYNTHETIC-PRIVATE-SESSION", "SYNTHETIC-CORRELATION", "SYNTHETIC-PULSE"):
            assert secret not in diagnostics
        return result, seen, auth.diagnostics
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_login_requests_existing_root_as_final_landing_not_baseinfo():
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    await auth.client.aclose()
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(200, text='<script>{"pageId":"qr-login","loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/authenticate"}</script>')

    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await auth.prepare_login()
        assert observed[0].url.params["redirectUri"] == "/"
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_email_then_optional_max_then_form_post_completes_one_login():
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    await auth.client.aclose()
    seen = []
    login_action = "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"
    email_action = "https://sso.mirea.ru/realms/mirea/login-actions/otp"

    def handler(request):
        fields = parse_qs(request.content.decode()) if request.content else {}
        seen.append((request.method, request.url.host, request.url.path, sorted(fields)))
        if request.url.host == "pulse.mirea.ru" and request.url.path == "/api/auth/login":
            return httpx.Response(302, headers={"location": login_action, "set-cookie": "Correlation=FIXTURE; Path=/; Secure"})
        if request.url.path.endswith("/authenticate") and request.method == "GET":
            return httpx.Response(200, text='<script>{"pageId":"qr-login","loginAction":"' + login_action + '"}</script>')
        if request.url.path.endswith("/authenticate"):
            return httpx.Response(200, text='<script>{"pageId":"email-code-form","loginAction":"' + email_action + '"}</script>')
        if request.url.path.endswith("/otp"):
            assert fields == {"emailCode": ["FIXTURE-EMAIL"], "login": ["true"]}
            context = {"pageId": "login-max-otp.ftl", "showSkip": True, "phoneNotRegistered": True, "url": {"loginAction": SOURCE}}
            return httpx.Response(200, text='<script>const kcContext = ' + json.dumps(context) + ';</script>')
        if request.url.path.endswith("/required-action"):
            assert fields == {"skip": ["true"]}
            return httpx.Response(200, text=form_page())
        if request.url.path == "/api/mireaauth":
            assert request.method == "POST"
            assert "Correlation=FIXTURE" in request.headers.get("cookie", "")
            return httpx.Response(302, headers={"location": "/", "set-cookie": ".AspNetCore.Cookies=FIXTURE-PULSE; Path=/; Secure"})
        if request.url.path == "/":
            return httpx.Response(200, json={"synthetic": True})
        raise AssertionError("unexpected request")

    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await auth.login("fixture@example.test", "FIXTURE-PASSWORD")
        assert result.challenge is not None
        result = await auth.complete_2fa(result.challenge, "FIXTURE-EMAIL")
        assert result.success, result.message
        assert result.cookies[".AspNetCore.Cookies"] == "FIXTURE-PULSE"
        assert len([r for r in seen if r[0] == "POST" and r[2].endswith("/required-action")]) == 1
        assert len([r for r in seen if r[0] == "POST" and r[2] == "/api/mireaauth"]) == 1
        report = json.dumps(auth.diagnostics)
        for secret in ("FIXTURE-PASSWORD", "FIXTURE-EMAIL", "SYNTHETIC-PRIVATE-CODE", "SYNTHETIC-PRIVATE-STATE", "FIXTURE-PULSE"):
            assert secret not in report
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_hybrid_oidc_id_token_is_returned_only_to_pulse_not_logged():
    fields = [("code", "SYNTHETIC-PRIVATE-CODE"), ("state", "SYNTHETIC-PRIVATE-STATE"),
              ("iss", "https://sso.mirea.ru/realms/mirea"), ("id_token", "SYNTHETIC-PRIVATE-ID-TOKEN")]
    result, seen, diagnostics = await run_form(form_page(fields=fields))
    assert result.success, result.message
    posts = [r for r in seen if r.method == "POST"]
    assert len(posts) == 1
    assert parse_qs(posts[0].content.decode())["id_token"] == ["SYNTHETIC-PRIVATE-ID-TOKEN"]
    assert "SYNTHETIC-PRIVATE-ID-TOKEN" not in json.dumps(diagnostics)


async def test_form_post_returns_to_pulse_with_correlation_and_no_token_exchange():
    result, seen, diagnostics = await run_form(form_page())
    assert result.success, result.message
    assert result.cookies[".AspNetCore.Cookies"] == "SYNTHETIC-PULSE"
    posts = [r for r in seen if r.method == "POST"]
    assert len(posts) == 1
    assert str(posts[0].url) == CALLBACK
    assert parse_qs(posts[0].content.decode()) == {"code": ["SYNTHETIC-PRIVATE-CODE"], "state": ["SYNTHETIC-PRIVATE-STATE"], "session_state": ["SYNTHETIC-PRIVATE-SESSION"]}
    assert not any(r.url.path.endswith("/token") for r in seen)
    assert any(d.get("phase") == "callback" for d in diagnostics)


@pytest.mark.parametrize("action", ["https://external.example/steal", "http://pulse.mirea.ru/api/mireaauth", "https://pulse.mirea.ru/not-callback", "https://pulse.mirea.ru:444/api/mireaauth", "https://user:pass@pulse.mirea.ru/api/mireaauth", "https://pulse.mirea.ru/api/mireaauth?extra=1"])
async def test_untrusted_callback_target_never_receives_form_values(action):
    result, seen, _ = await run_form(form_page(action))
    assert not result.success
    assert seen == []


@pytest.mark.parametrize("fields", [[("code", "x")], [("state", "x")], [("code", "x"), ("state", "")], [("code", "x"), ("state", "y"), ("state", "z")], [("code", "x"), ("state", "y"), ("password", "DO-NOT-SEND")]])
async def test_invalid_callback_fields_are_not_submitted(fields):
    result, seen, _ = await run_form(form_page(fields=fields))
    assert not result.success
    assert seen == []


async def test_get_form_is_not_oidc_form_post():
    result, seen, _ = await run_form(form_page(method="get"))
    assert not result.success
    assert seen == []


async def test_ambiguous_callback_forms_are_not_submitted():
    result, seen, _ = await run_form(form_page() + form_page())
    assert not result.success
    assert seen == []


async def test_callback_repetition_is_stopped_after_one_post():
    result, seen, _ = await run_form(form_page(), repeated=True)
    assert not result.success
    assert len([r for r in seen if r.method == "POST"]) == 1


async def test_unknown_page_records_structure_but_never_values():
    result, seen, diagnostics = await run_form(form_page("https://external.example/steal"))
    assert not result.success
    assert seen == []
    assert any(d.get("form_count") == 1 for d in diagnostics)
