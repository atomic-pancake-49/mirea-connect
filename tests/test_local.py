"""Локальный режим: вход через страницу на 127.0.0.1, сессия на диске, установщик."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from pymirea import _settings
from pymirea.auth import AuthChallenge, AuthResult, MireaAuth
from pymirea.store import SessionStore

from mirea_connect import installer, services
from mirea_connect.local import build_local


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMIREA_SESSION_KEYS", raising=False)
    prev = _settings._cfg
    _settings._cfg = None
    yield SessionStore(tmp_path / "home")
    _settings._cfg = prev


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


async def test_local_login_page_and_personal_tool(store, monkeypatch):
    ch = AuthChallenge(kind="otp", action_url="https://sso.mirea.ru/x", field_name="otp", hidden_fields={})

    async def fake_login(self, u, p):
        return AuthResult(success=False, message="otp", challenge=ch)

    async def fake_2fa(self, c, code, *, cookies=None):
        if code != "111111":
            return AuthResult(success=False, message="Неверный код", challenge=c)
        return AuthResult(success=True, message="ok", cookies={"K": "v"})

    async def fake_grades(cookies):
        assert cookies["K"] == "v"
        return {"subjects": [{"name": "Физика"}]}

    monkeypatch.setattr(MireaAuth, "login", fake_login)
    monkeypatch.setattr(MireaAuth, "complete_2fa", fake_2fa)
    monkeypatch.setattr(services, "grades", fake_grades)

    mcp, page = build_local(store)
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="login"):
        await mcp.call_tool("my_grades", {})

    res = await mcp.call_tool("login", {"open_browser": False})
    url = json.loads(_text(res))["url"]
    assert url.startswith("http://127.0.0.1:")
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(url)
            assert "Вход в МИРЭА" in r.text
            # без секрета в пути страница недоступна
            base = re.match(r"(http://127.0.0.1:\d+)", url).group(1)
            assert (await c.get(base + "/")).status_code == 404
            r = await c.post(url + "/login", data={"username": "me@edu.mirea.ru", "password": "pw"})
            assert "Код подтверждения" in r.text
            r = await c.post(url + "/otp", data={"code": "000000"})
            assert "Неверный код" in r.text
            r = await c.post(url + "/otp", data={"code": "111111"})
            assert "Готово" in r.text
    finally:
        await page.stop()

    assert store.load().login == "me@edu.mirea.ru"
    res = await mcp.call_tool("my_grades", {})
    assert "Физика" in _text(res)
    res = await mcp.call_tool("whoami", {})
    assert '"logged_in": true' in _text(res)
    await mcp.call_tool("delete_my_data", {})
    assert not store.exists()


def test_installer_writes_configs(tmp_path, monkeypatch):
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    desktop.parent.mkdir()
    desktop.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: desktop)
    monkeypatch.setattr(installer, "cursor_config", lambda: cursor)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)

    assert installer.install(True) == 0
    d = json.loads(desktop.read_text())
    assert d["keep"] == 1 and "other" in d["mcpServers"]
    assert d["mcpServers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "mirea" in json.loads(cursor.read_text())["mcpServers"]

    installer.install(False)
    assert "mirea" not in json.loads(desktop.read_text())["mcpServers"]
    assert "other" in json.loads(desktop.read_text())["mcpServers"]
