"""Optional MAX setup must honor the explicit server-offered skip, once.

Synthetic HTTP only: no account, session store, filesystem fixture or live SSO.
"""
import json
from urllib.parse import parse_qs

import httpx
import pytest
from pymirea import _settings
from pymirea.config import Config

from mirea_connect.auth import MireaAuth

ACTION = "https://sso.mirea.ru/realms/mirea/login-actions/required-action?execution=SYNTHETIC"


def max_page(**overrides):
    context = {
        "pageId": "login-max-otp.ftl",
        "isEnrollment": True,
        "showSkip": True,
        "phoneNotRegistered": False,
        "noPhoneNumber": False,
        "deliveryPending": False,
        "sendFailed": False,
        "url": {"loginAction": ACTION},
    }
    context.update(overrides)
    return "<script>window.kcContext = " + json.dumps(context) + ";</script>"


async def run_max(html, *, repeated=False):
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    await auth.client.aclose()
    seen = []

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path,
                     parse_qs(request.content.decode()) if request.content else {}))
        if request.url.path.endswith("required-action"):
            if repeated:
                return httpx.Response(200, text=html)
            return httpx.Response(302, headers={"location": "https://pulse.mirea.ru/signin-oidc?code=SYNTHETIC&state=SYNTHETIC"})
        if request.url.path == "/signin-oidc":
            return httpx.Response(302, headers={"location": "/api/baseinfo", "set-cookie": ".AspNetCore.Cookies=SYNTHETIC; Path=/; Secure"})
        if request.url.path == "/api/baseinfo":
            return httpx.Response(200, json={"synthetic": True})
        raise AssertionError("unexpected request")

    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = httpx.Response(200, text=html, request=httpx.Request(
            "POST", ACTION, data={"emailCode": "PRIVATE-SYNTHETIC-CODE", "login": "true"}))
        result = await auth._result(response)
        diagnostics = json.dumps(auth.diagnostics)
        assert "PRIVATE-SYNTHETIC-CODE" not in diagnostics
        assert "SYNTHETIC&state" not in diagnostics
        return result, seen, auth.diagnostics
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_optional_max_skip_continues_pulse_owned_callback_once():
    result, seen, diagnostics = await run_max(max_page())
    assert result.success, result.message
    assert result.cookies[".AspNetCore.Cookies"] == "SYNTHETIC"
    posts = [row for row in seen if row[0] == "POST"]
    assert posts == [("POST", "sso.mirea.ru", "/realms/mirea/login-actions/required-action", {"skip": ["true"]})]
    assert not any(row[2].endswith("/token") for row in seen)
    assert any(row.get("phase") == "skip" for row in diagnostics)


@pytest.mark.parametrize("flags", [
    {"showSkip": False}, {"showSkip": None}, {"showSkip": "true"},
    {"showSkip": 1}, {"deliveryPending": True}, {"sendFailed": True},
    {"isEnrollment": False},
])
async def test_absent_or_not_visible_skip_never_posts(flags):
    result, seen, _ = await run_max(max_page(**flags))
    assert not result.success
    assert seen == []


async def test_missing_skip_flag_never_posts():
    context = {"pageId": "login-max-otp.ftl", "isEnrollment": True,
               "url": {"loginAction": ACTION}}
    result, seen, _ = await run_max("<script>window.kcContext = " + json.dumps(context) + ";</script>")
    assert not result.success
    assert seen == []


async def test_server_repeating_optional_max_is_bounded_to_one_skip():
    result, seen, _ = await run_max(max_page(), repeated=True)
    assert not result.success
    assert len([row for row in seen if row[0] == "POST"]) == 1


async def test_optional_skip_rejects_untrusted_action():
    result, seen, _ = await run_max(max_page(url={"loginAction": "https://external.example/steal"}))
    assert not result.success
    assert seen == []


async def test_skip_marker_on_another_page_is_not_authority():
    result, seen, _ = await run_max(max_page(pageId="login-otp.ftl"))
    assert not result.success
    assert seen == []


async def test_keycloakify_comments_and_methods_preserve_top_level_flags():
    html = '''<script>const kcContext = {
        "pageId": "login-max-otp.ftl",
        /* class: org.keycloak.forms.login.freemarker.model.AuthenticationContextBean */
        "auth": {"showSkip": false, "other": {"value": "}; not syntax"}},
        "messagesPerField": {get: function(field) {return "unused";}},
        "isEnrollment": true,
        "showSkip": true,
        "deliveryPending": false,
        "sendFailed": false,
        "url": {"loginAction": "''' + ACTION + '''"},
    };</script>'''
    result, seen, _ = await run_max(html)
    assert result.success, result.message
    assert len([row for row in seen if row[0] == "POST"]) == 1


async def test_commented_skip_flag_is_not_authority():
    html = '''<script>const kcContext = {
        "pageId": "login-max-otp.ftl", "isEnrollment": true,
        /* "showSkip": true, */
        "url": {"loginAction": "''' + ACTION + '''"}
    };</script>'''
    result, seen, _ = await run_max(html)
    assert not result.success
    assert seen == []


async def test_nested_incidental_skip_marker_is_not_authority():
    context = {"pageId": "login-max-otp.ftl", "isEnrollment": True,
               "unrelated": {"showSkip": True}, "url": {"loginAction": ACTION}}
    result, seen, _ = await run_max("<script>window.kcContext = " + json.dumps(context) + ";</script>")
    assert not result.success
    assert seen == []
