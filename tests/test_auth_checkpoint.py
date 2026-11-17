"""RED repros for retained callback state; synthetic transport, no stores/files."""
import json

import httpx
from pymirea import _settings
from pymirea.auth import AuthChallenge
from pymirea.config import Config

from mirea_connect.auth import MireaAuth
from mirea_connect.session_state import LocalSessionStore

CALLBACK = "https://pulse.mirea.ru/api/mireaauth"
SOURCE = "https://sso.mirea.ru/realms/mirea/login-actions/otp"
FORM = '<form method="post" action="' + CALLBACK + '"><input type="hidden" name="code" value="PRIVATE-CALLBACK-CODE"><input type="hidden" name="state" value="PRIVATE-CALLBACK-STATE"></form>'


async def run_callback(*, landing_raises=False, landing_reauth=False, checkpoint_fails=False, poisoned_landing=False, sink=None):
    previous = _settings._cfg
    _settings.configure(Config(session_keys="synthetic-test-key-only"))
    auth = MireaAuth()
    await auth.client.aclose()
    checkpoints = []
    observations = {}

    def checkpoint(cookies):
        if checkpoint_fails:
            raise OSError("PRIVATE-STORAGE-ERROR")
        checkpoints.append(dict(cookies))
        return sink(cookies) if sink else "synthetic-candidate-id"

    # Existing code accepts this assignment but never invokes it: behavioral RED.
    auth.checkpoint_handler = checkpoint

    def handler(request):
        if request.url.path.endswith("/otp"):
            return httpx.Response(200, text=FORM)
        if request.url.host == "pulse.mirea.ru" and request.url.path == "/api/mireaauth":
            return httpx.Response(302, headers={"location": "/synthetic-landing", "set-cookie": ".AspNetCore.Cookies=PRIVATE-COOKIE-VALUE; Path=/; Secure; HttpOnly"})
        if request.url.host == "pulse.mirea.ru" and request.url.path == "/synthetic-landing":
            observations["checkpoint_before_landing"] = bool(checkpoints)
            if poisoned_landing:
                return httpx.Response(302, headers={"location": "https://sso.mirea.ru/realms/mirea/login-actions/authenticate",
                                                   "set-cookie": ".AspNetCore.Cookies=PRIVATE-POISONED-COOKIE; Path=/; Secure"})
            if landing_reauth:
                return httpx.Response(302, headers={"location": "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"})
            if landing_raises:
                raise httpx.ReadTimeout("synthetic landing failure", request=request)
            return httpx.Response(404, text="synthetic404")
        if request.url.host == "sso.mirea.ru" and request.url.path.endswith("/authenticate"):
            return httpx.Response(200, text='<script>{"pageId":"email-code-form","loginAction":"' + SOURCE + '"}</script>')
        raise AssertionError("No external network allowed")

    auth.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    challenge = AuthChallenge(kind="email_code", action_url=SOURCE, field_name="emailCode", hidden_fields={"login": "true"})
    try:
        result = await auth.complete_2fa(challenge, "PRIVATE-EMAIL-CODE")
        text = json.dumps(auth.diagnostics)
        observations["checkpoint_error"] = auth.checkpoint_error
        observations["candidate_id"] = auth.candidate_id
        for secret in ("PRIVATE-COOKIE-VALUE", "PRIVATE-CALLBACK-CODE", "PRIVATE-CALLBACK-STATE", "PRIVATE-EMAIL-CODE"):
            assert secret not in text
        return result, checkpoints, observations
    finally:
        await auth.close()
        _settings._cfg = previous


async def test_callback_cookie_is_checkpointed_before404_landing():
    result, checkpoints, observations = await run_callback()
    assert observations["checkpoint_before_landing"] is True
    assert checkpoints[0][".AspNetCore.Cookies"] == "PRIVATE-COOKIE-VALUE"
    assert result.cookies and result.cookies[".AspNetCore.Cookies"] == "PRIVATE-COOKIE-VALUE"
    assert result.challenge is None


async def test_network_failure_after_callback_does_not_reask_consumed_email_code():
    result, checkpoints, observations = await run_callback(landing_raises=True)
    assert result.challenge is None, "post-callback transport failure must be terminal, not an OTP retry"
    assert observations["checkpoint_before_landing"] is True
    assert checkpoints[0][".AspNetCore.Cookies"] == "PRIVATE-COOKIE-VALUE"
    assert result.cookies and result.cookies[".AspNetCore.Cookies"] == "PRIVATE-COOKIE-VALUE"


async def test_reauthentication_page_after_callback_does_not_replay_email_code():
    result, checkpoints, _ = await run_callback(landing_reauth=True)
    assert result.challenge is None
    assert checkpoints and result.cookies


async def test_failed_checkpoint_stops_before_landing_and_never_claims_saved():
    result, checkpoints, observations = await run_callback(checkpoint_fails=True)
    assert not result.success and result.challenge is None
    assert observations["checkpoint_error"] is True
    assert checkpoints == []
    assert "checkpoint_before_landing" not in observations


async def test_poisoned_landing_cannot_supersede_default_prelanding_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMIREA_SESSION_KEYS", "synthetic-checkpoint-preservation-key")
    store = LocalSessionStore(tmp_path / "session")
    result, checkpoints, observations = await run_callback(poisoned_landing=True, sink=store.save_candidate)
    assert result.challenge is None
    assert len(checkpoints) == 1, "one immutable recovery candidate per callback"
    assert result.cookies[".AspNetCore.Cookies"] == "PRIVATE-COOKIE-VALUE"
    assert store.load_candidate().candidate_id == observations["candidate_id"]
    assert store.load_candidate().cookies[".AspNetCore.Cookies"] == "PRIVATE-COOKIE-VALUE"
