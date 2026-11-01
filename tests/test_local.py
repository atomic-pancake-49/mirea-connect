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
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "no-openclaw"))

    assert installer.install(True) == 0
    d = json.loads(desktop.read_text())
    assert d["keep"] == 1 and "other" in d["mcpServers"]
    assert d["mcpServers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "mirea" in json.loads(cursor.read_text())["mcpServers"]

    installer.install(False)
    assert "mirea" not in json.loads(desktop.read_text())["mcpServers"]
    assert "other" in json.loads(desktop.read_text())["mcpServers"]


def test_codex_toml_roundtrip():
    import tomllib

    src = 'model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n\n[mcp_servers.mirea]\ncommand = "old"\n\n[mcp_servers.mirea.env]\nA = "1"\n\n[profiles.p]\nmodel = "y"\n'
    out, changed = installer.edit_codex_toml(src, True)
    data = tomllib.loads(out)
    assert changed
    assert data["model"] == "o4" and data["profiles"]["p"]["model"] == "y"
    assert data["mcp_servers"]["other"]["command"] == "x"
    assert data["mcp_servers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "env" not in data["mcp_servers"]["mirea"]
    out2, _ = installer.edit_codex_toml(out, True)
    assert out2.count("[mcp_servers.mirea]") == 1
    removed, changed = installer.edit_codex_toml(out2, False)
    assert changed and "mirea" not in tomllib.loads(removed)["mcp_servers"]
    assert installer.edit_codex_toml("", True)[0].startswith("[mcp_servers.mirea]")


def test_hermes_yaml_roundtrip():
    yaml = pytest.importorskip("yaml")
    src = (
        "model: hermes-4\n"
        "mcp_servers:\n"
        "    # comment\n"
        "    filesystem:\n"
        "        command: npx\n"
        "        args: [\"-y\", \"x\"]\n"
        "    mirea:\n"
        "        command: old\n"
        "        args: []\n"
        "toolsets:\n"
        "  - web\n"
    )
    out, changed = installer.edit_hermes_yaml(src, True)
    data = yaml.safe_load(out)
    assert changed and data["model"] == "hermes-4" and data["toolsets"] == ["web"]
    assert data["mcp_servers"]["filesystem"]["command"] == "npx"
    assert data["mcp_servers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "# comment" in out
    out2, _ = installer.edit_hermes_yaml(out, True)
    assert out2.count("mirea:") == 1
    removed, changed = installer.edit_hermes_yaml(out2, False)
    assert changed and "mirea" not in yaml.safe_load(removed)["mcp_servers"]

    fresh, _ = installer.edit_hermes_yaml("model: x\n", True)
    assert yaml.safe_load(fresh)["mcp_servers"]["mirea"]["command"]
    empty, _ = installer.edit_hermes_yaml("mcp_servers: {}\n", True)
    assert "mirea" in yaml.safe_load(empty)["mcp_servers"]


def test_installer_codex_hermes_openclaw(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: tmp_path / "nope" / "c.json")
    monkeypatch.setattr(installer, "cursor_config", lambda: tmp_path / "nope2" / "mcp.json")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text("model: x\n")
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/bin/openclaw" if n == "openclaw" else None)

    class R:
        returncode = 0
        stdout = stderr = ""

    def fake_run(args, **kw):
        calls.append(args)
        return R()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer.install(True)
    assert "[mcp_servers.mirea]" in (tmp_path / ".codex" / "config.toml").read_text()
    assert "mirea:" in (tmp_path / ".hermes" / "config.yaml").read_text()
    oc = [c for c in calls if c[0] == "/bin/openclaw"]
    assert oc and oc[0][1:4] == ["mcp", "set", "mirea"]
    assert json.loads(oc[0][4])["args"][-1] == "local"
