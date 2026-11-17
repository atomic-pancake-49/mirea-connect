"""CLI must not ask forever when SSO repeats a challenge; no real credentials."""
from types import SimpleNamespace

from pymirea.auth import AuthChallenge, AuthResult

from mirea_connect import auth, cli


async def test_cli_limits_repeated_otp_challenge(monkeypatch):
    challenge = AuthChallenge(kind="email_code", action_url="https://sso.mirea.ru/realms/mirea/login-actions/otp", field_name="emailCode", hidden_fields={})
    calls = []

    class FakeAuth:
        async def login(self, username, password):
            return AuthResult(False, "needs code", challenge=challenge)

        async def complete_2fa(self, current, code):
            calls.append(True)
            if len(calls) > 3:
                raise AssertionError("infinite OTP retry loop")
            return AuthResult(False, "needs code", challenge=challenge)

        async def close(self):
            pass

    monkeypatch.setattr(auth, "MireaAuth", FakeAuth)
    monkeypatch.setattr(cli, "_store", lambda: SimpleNamespace())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-not-secret")
    monkeypatch.setattr("builtins.input", lambda prompt: "synthetic-not-secret")
    assert await cli._login_terminal("synthetic@example.test") == 1
    assert len(calls) == 3
