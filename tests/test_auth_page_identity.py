"""Page identity must beat incidental OTP marker strings; synthetic HTML only."""
import httpx
import pytest
from pymirea import _settings
from pymirea.config import Config

from mirea_connect.auth import MireaAuth


@pytest.mark.parametrize("page_id", ["email-code-form", "email-code-form.ftl"])
async def test_email_page_is_not_misclassified_as_totp(page_id):
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    try:
        html = '<script>{"pageId":"' + page_id + '","otpLogin":{},"url":{"loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/otp"}}</script>'
        response = httpx.Response(200, text=html, request=httpx.Request("POST", "https://sso.mirea.ru/realms/mirea/login-actions/authenticate", data={"username": "synthetic", "password": "synthetic"}))
        result = await auth._result(response)
        assert result.challenge is not None
        assert result.challenge.kind == "email_code"
        assert result.challenge.field_name == "emailCode"
        assert result.challenge.hidden_fields == {"login": "true"}
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_plain_login_page_is_never_treated_as_otp_due_to_incidental_marker():
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    try:
        html = '<script>{"pageId":"qr-login","otpLogin":{},"url":{"loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/authenticate"}}</script>'
        response = httpx.Response(200, text=html, request=httpx.Request("GET", "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"))
        result = await auth._result(response)
        assert result.challenge is None
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_diagnostic_records_only_shape_not_credentials():
    import json

    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    try:
        response = httpx.Response(200, text='<script>{"pageId":"email-code-form","loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/otp"}</script>', request=httpx.Request("POST", "https://sso.mirea.ru/realms/mirea/login-actions/otp", data={"emailCode": "PRIVATE-CODE", "login": "true"}))
        await auth._result(response)
        report = json.dumps(auth.diagnostics)
        assert "PRIVATE-CODE" not in report
        assert auth.diagnostics[-1]["submitted_fields"] == ["emailCode", "login"]
        assert auth.diagnostics[-1]["challenge_kind"] == "email_code"
    finally:
        await auth.close()
        _settings._cfg = previous


@pytest.mark.parametrize("enrollment", [True, False])
async def test_max_is_distinct_from_email_and_does_not_prompt_during_enrollment(enrollment):
    previous = _settings._cfg
    _settings.configure(Config(session_keys="test-not-used"))
    auth = MireaAuth()
    try:
        html = '<script>{"pageId":"login-max-otp","isEnrollment":' + str(enrollment).lower() + ',"loginAction":"https://sso.mirea.ru/realms/mirea/login-actions/otp"}</script>'
        challenge = auth._extract_otp_challenge(html)
        if enrollment:
            assert challenge is None
        else:
            assert challenge.kind == "max_otp" and challenge.field_name == "code"
    finally:
        await auth.close()
        _settings._cfg = previous

