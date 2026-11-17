"""CLI + local MCP contract tests; synthetic network, persistent test artifacts."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pymirea.exceptions import MireaSessionExpired
from pymirea.store import NotLoggedIn

from mirea_connect import cli, local, services
from mirea_connect import material_commands as commands
from mirea_connect.sessions import NoSession


class MemoryStore:
    def __init__(self, logged_in=True):
        self.logged_in = logged_in

    def configure(self):
        pass

    def load(self):
        if not self.logged_in:
            raise NotLoggedIn("нет входа")
        return SimpleNamespace(cookies={"synthetic": "not-a-real-session"})

    def save(self, cookies, **kwargs):
        pass

    def clear(self):
        raise AssertionError("must preserve stored session")


@pytest.fixture
def setup_export(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        course_id = request.url.params.get("id")
        if request.url.path == "/course/view.php":
            return httpx.Response(200, text=f'<li class="activity modtype_resource" id="module-{course_id}"><a href="/mod/resource/view.php?id={course_id}">File</a></li>', headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"synthetic-file", headers={"content-type": "application/octet-stream", "content-disposition": 'attachment; filename="sample.txt"'})

    class FakeSDO:
        def __init__(self, session_cookies):
            self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def get_courses(self):
            return SimpleNamespace(success=True, courses=[SimpleNamespace(id=1, name="Course A"), SimpleNamespace(id=2, name="Course B")])

        async def close(self):
            await self.client.aclose()

    async def fresh(cookies, **kwargs):
        pass

    root = Path(__file__).parent / "artifacts" / uuid4().hex
    monkeypatch.setattr(commands, "MireaSDO", FakeSDO)
    monkeypatch.setattr(commands, "EXPORT_DIR", root.absolute())
    monkeypatch.setattr(local, "ensure_fresh_token", fresh)
    return root, seen


@pytest.mark.parametrize("course_id, expected", [(None, 2), (2, 1)])
async def test_export_scope_and_persisted_result(setup_export, course_id, expected):
    root, seen = setup_export
    result = await commands.export_for_store(MemoryStore(), course_id)
    assert result["status"] == "complete"
    assert result["courses"] == expected
    archive, manifest = Path(result["archive"]), Path(result["manifest"])
    assert archive.is_file() and manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data["totals"]["courses"] == expected
    assert "not-a-real-session" not in manifest.read_text()
    assert archive.parent == root
    if course_id == 2:
        assert not any("id=1" in url for url in seen)


async def test_repeated_exports_keep_first_archive(setup_export):
    first = await commands.export_for_store(MemoryStore(), 1)
    original = Path(first["archive"]).read_bytes()
    second = await commands.export_for_store(MemoryStore(), 1)
    assert first["archive"] != second["archive"]
    assert Path(first["archive"]).read_bytes() == original


async def test_unknown_course_does_not_create_output(setup_export):
    root, seen = setup_export
    with pytest.raises(services.ServiceError, match="не найден"):
        await commands.export_for_store(MemoryStore(), 999)
    assert not root.exists() and not seen


async def test_missing_login_preserves_store(setup_export):
    with pytest.raises(NoSession):
        await commands.export_for_store(MemoryStore(False))


async def test_expired_login_preserves_store(setup_export, monkeypatch):
    async def expired(cookies, **kwargs):
        raise MireaSessionExpired("expired")

    monkeypatch.setattr(local, "ensure_fresh_token", expired)
    with pytest.raises(NoSession, match="истекла"):
        await commands.export_for_store(MemoryStore())
    local.LocalSource(MemoryStore()).mark_dead()


async def test_local_mcp_calls_same_export(setup_export):
    mcp, _ = local.build_local(MemoryStore())
    response = await mcp.call_tool("export_materials", {"course": "Course B"})
    text = "".join(getattr(item, "text", "") for item in response.content)
    result = json.loads(text)
    assert result["status"] == "complete" and result["courses"] == 1
    assert Path(result["archive"]).is_file()


@pytest.mark.parametrize("args, selected", [(["materials", "--all", "--json"], None), (["materials", "--course", "Большие данные", "--json"], "Большие данные")])
def test_cli_scope(args, selected, monkeypatch, capsys):
    async def export(store, course_id=None):
        assert course_id == selected
        return {"status": "complete", "scope": "all" if course_id is None else f"course:{course_id}", "archive": "/synthetic/archive.zip"}

    monkeypatch.setattr(commands, "export_for_store", export)
    monkeypatch.setattr(cli, "_store", MemoryStore)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


@pytest.mark.parametrize("args", [["materials"], ["materials", "--all", "--course", "1"], ["materials", "--no-browser"]])
def test_cli_requires_exact_scope(args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_cli_partial_has_nonzero_exit(monkeypatch, capsys):
    async def export(store, course_id=None):
        return {"status": "partial"}

    monkeypatch.setattr(commands, "export_for_store", export)
    monkeypatch.setattr(cli, "_store", MemoryStore)
    assert cli.main(["materials", "--all", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_exclusive_file_creation(setup_export):
    root, _ = setup_export
    commands._private_dir(root)
    path = root / "keep.txt"
    commands._new_file(path, b"keep")
    with pytest.raises(FileExistsError):
        commands._new_file(path, b"overwrite")
    assert path.read_bytes() == b"keep"


@pytest.mark.parametrize("query", ["Большие данные", "большие ДАННЫЕ", "  большие   данные  ", "данные"])
def test_select_course_by_name(query):
    courses = [SimpleNamespace(id=1, name="Большие данные"), SimpleNamespace(id=2, name="Схемотехника электронных устройств")]
    assert commands.select_courses(courses, query) == [courses[0]]


def test_ambiguous_course_lists_names_instead_of_guessing():
    courses = [SimpleNamespace(id=1, name="Аналоговая и цифровая схемотехника"), SimpleNamespace(id=2, name="Схемотехника электронных устройств")]
    with pytest.raises(services.ServiceError, match="неоднозначно") as error:
        commands.select_courses(courses, "схемотехника")
    assert all(c.name in str(error.value) for c in courses)


def test_unknown_course_lists_available_names():
    courses = [SimpleNamespace(id=1, name="Большие данные")]
    with pytest.raises(services.ServiceError, match="Большие данные"):
        commands.select_courses(courses, "нет такого курса")


async def test_name_export_uses_resolved_course_only(setup_export):
    _, seen = setup_export
    result = await commands.export_for_store(MemoryStore(), "course b")
    assert result["course_names"] == ["Course B"]
    assert result["courses"] == 1
    assert not any("id=1" in url for url in seen)

