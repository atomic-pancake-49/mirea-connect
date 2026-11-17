"""Upgraded pymirea must stay visible through the connector contract."""
import importlib.metadata

from mirea_connect.local import build_local
from mirea_connect.session_state import LocalSessionStore


async def test_grades_schema_uses_server_maxima_not_fixed_legacy_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMIREA_SESSION_KEYS", "synthetic-schema-key")
    store = LocalSessionStore(tmp_path / "session")
    server, _ = build_local(store)
    tools = await server.list_tools()
    tool = next(t for t in tools if t.name == "my_grades")
    assert "семестровый (из 30)" not in tool.description
    assert "посещения (из 30)" not in tool.description
    assert "*_max" in tool.description


def test_installed_dependency_is_upstream_051():
    assert importlib.metadata.version("pymirea") == "0.5.1"
