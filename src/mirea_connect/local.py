"""Локальный режим: MCP по stdio, вход и сессия — только на устройстве пользователя.

Сессия хранится тем же способом, что и в CLI pymirea (``~/.config/pymirea``,
зашифровано, права 0600), поэтому ``pymirea login`` и ``mirea-connect login``
взаимозаменяемы. Для входа без терминала есть инструмент ``login``: он
поднимает страницу на ``127.0.0.1`` со случайным адресом и открывает браузер.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import secrets
import socket
import time
import webbrowser
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pymirea.auth import AuthChallenge
from pymirea.exceptions import MireaSessionExpired
from pymirea.store import NotLoggedIn, SessionStore
from pymirea.tokens import ensure_fresh_token
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from . import services as S
from .auth import MireaAuth
from .login_session import verify_candidate, write_diagnostic
from .session_state import LocalSessionStore, OperationCookies
from .sessions import NoSession
from .tools import BASE_INSTRUCTIONS, LOCAL_ACTION, register_tools
from .web import _err, _page

logger = logging.getLogger(__name__)

LOGIN_PAGE_TTL_S = 15 * 60

INSTRUCTIONS = BASE_INSTRUCTIONS + (
    " Коннектор работает локально на устройстве пользователя. Если личные инструменты (my_*) сообщают, "
    "что входа нет или сессия истекла, сначала вызови verify_login: он проверяет сохранённого кандидата "
    "без пароля и нового кода. Только если кандидата нет, предложи login или mirea-connect login."
)

RELOGIN_HINT = (
    "Сначала verify_login (или `mirea-connect verify-login`) для сохранённого кандидата без нового кода. "
    "Только если кандидата нет, используйте login или `mirea-connect login`."
)


class LocalSource:
    relogin_hint = RELOGIN_HINT

    def __init__(self, store: SessionStore, *, clear_on_expiry: bool = False) -> None:
        self.store = store
        self.clear_on_expiry = clear_on_expiry  # False: истёкшая сессия остаётся на диске, просто нужен вход
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def use(self) -> AsyncIterator[dict]:
        async with self._lock:
            try:
                stored = self.store.load()
            except NotLoggedIn as e:
                raise NoSession("Вход в МИРЭА не выполнен.") from e
            cookies = OperationCookies(stored.cookies)
            before = json.dumps(cookies, sort_keys=True, default=str)
            try:
                await ensure_fresh_token(cookies, buffer_s=60)
            except MireaSessionExpired as e:
                raise NoSession("Сессия МИРЭА истекла.") from e
            yield cookies
            # Only a successful operation can persist refreshed state. Exceptions
            # (including cancellation) must preserve the previous verified record.
            if cookies.persist_allowed and json.dumps(cookies, sort_keys=True, default=str) != before:
                self.store.save(cookies)

    def whoami(self) -> dict:
        try:
            stored = self.store.load()
        except NotLoggedIn:
            return {"mode": "local", "logged_in": False, "personal_tools": False}
        return {"mode": "local", "logged_in": True, "login": stored.login, "personal_tools": True, "server_checked": False}

    def forget(self) -> None:
        raise S.ServiceError("Удаление локальной сессии отключено; все версии сохранены.")

    def mark_dead(self) -> None:
        pass  # Retain expired records; never erase evidence during a read.


# --- локальная страница входа ---


class _NoSignalServer:
    """uvicorn.Server без перехвата сигналов — их обрабатывает MCP-процесс."""

    def __new__(cls, config):
        import uvicorn

        class S(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        return S(config)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalLogin:
    """Одноразовая страница входа на 127.0.0.1 со случайным секретом в пути."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.url: str | None = None
        self.done = asyncio.Event()
        self._secret = ""
        self._server = None
        self._task: asyncio.Task | None = None
        self._auth: MireaAuth | None = None
        self._challenge: AuthChallenge | None = None
        self._login = ""
        self._attempts = 0
        self._otp_attempts = 0
        self._started = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> str:
        if self.running and self.url and time.time() - self._started < LOGIN_PAGE_TTL_S:
            return self.url
        await self.stop()
        import uvicorn

        self._secret = secrets.token_urlsafe(18)
        self._attempts = 0
        self._otp_attempts = 0
        self.done = asyncio.Event()
        base = f"/{self._secret}"
        app = Starlette(
            routes=[
                Route(base, self._get, methods=["GET"]),
                Route(f"{base}/login", self._post_login, methods=["POST"]),
                Route(f"{base}/otp", self._post_otp, methods=["POST"]),
            ]
        )
        port = _free_port()
        self._host = f"127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="off")
        self._server = _NoSignalServer(config)
        self._task = asyncio.create_task(self._server.serve())
        self._started = time.time()
        asyncio.get_running_loop().call_later(LOGIN_PAGE_TTL_S, lambda: asyncio.ensure_future(self.stop()))
        for _ in range(50):
            if getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.05)
        self.url = f"http://127.0.0.1:{port}{base}"
        return self.url

    async def stop(self) -> None:
        if self._auth:
            with contextlib.suppress(OSError, ValueError):
                write_diagnostic(self._auth.diagnostics, self.store)
            with contextlib.suppress(Exception):
                await self._auth.close()
        self._auth = None
        self._challenge = None
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=3)
        self._server = None
        self._task = None
        self.url = None

    # pages

    def _form(self, error: str | None = None, login: str = "") -> HTMLResponse:
        body = f"""<div class="card">
<h1>Вход в МИРЭА</h1>
<p class="muted">Страница открыта коннектором MIREA Connect на этом компьютере. Логин и пароль уходят только на sso.mirea.ru.</p>
{_err(error)}
<form method="post" action="/{self._secret}/login">
<label for="u">Логин</label><input id="u" name="username" type="email" placeholder="ivanov.i.i@edu.mirea.ru" value="{html.escape(login)}" required autocomplete="username">
<label for="p">Пароль</label><input id="p" name="password" type="password" required autocomplete="current-password">
<button>Войти</button>
</form></div>"""
        return _page("Вход в МИРЭА", body)

    def _otp(self, error: str | None = None) -> HTMLResponse:
        kind = self._challenge.kind if self._challenge else None
        delivery = {"email_code": "Код из письма на привязанную почту.", "max_otp": "Код из MAX.",
                    "otp": "Код из приложения-аутентификатора."}.get(kind, "Подтверждение SSO.")
        body = f"""<div class="card">
<h1>Код подтверждения</h1>
<p class="muted">{delivery}</p>
{_err(error)}
<form method="post" action="/{self._secret}/otp">
<label for="c">Код</label><input id="c" name="code" type="password" inputmode="numeric" autocomplete="one-time-code" required autofocus>
<button>Подтвердить</button>
</form></div>"""
        return _page("Код подтверждения", body)

    def _ok(self) -> HTMLResponse:
        body = """<div class="card"><h1>Готово</h1>
<p>Вы вошли в МИРЭА. Сессия сохранена на этом компьютере. Вкладку можно закрыть и вернуться в чат.</p></div>"""
        return _page("Готово", body)

    async def _finish(self, cookies: dict) -> Response:
        self._challenge = None
        if self._auth and self._auth.checkpoint_error:
            return self._form("Не удалось сохранить кандидата; вход не объявлен успешным.")
        try:
            ident = self._auth.candidate_id if self._auth else None
            ident = ident or self.store.save_candidate(cookies, login=self._login)
            answer = await verify_candidate(self.store, ident)
        except (OSError, ValueError, NotLoggedIn):
            return self._form("Не удалось сохранить или проверить кандидата; прежние файлы не изменены.")
        title = "Готово" if answer["status"] == "verified" else "Кандидат сохранён"
        if answer["status"] == "verified":
            self.done.set()
            asyncio.get_running_loop().call_later(2, lambda: asyncio.ensure_future(self.stop()))
        pulse = "проверен" if answer["pulse"]["status"] == "verified" else "не подтверждён"
        sdo = "проверена" if answer["sdo"]["status"] == "verified" else "не подтверждена"
        command = html.escape("mirea-connect verify-login " + answer["candidate_id"])
        return _page(title, f'<div class="card"><h1>{title}</h1><p>Pulse: {pulse}. СДО: {sdo}.</p><p>Повторная проверка без пароля и нового кода:</p><code>{command}</code></div>')

    def _foreign(self, request: Request) -> bool:
        """Чужой Host (DNS rebinding) или POST с чужого сайта. Origin у этих страниц бывает «null»
        из-за referrer=no-referrer, поэтому принимаем его; путь всё равно знает только владелец ссылки."""
        if request.headers.get("host") != getattr(self, "_host", None):
            return True
        origin = request.headers.get("origin")
        return request.method == "POST" and origin not in (None, "null", f"http://{self._host}")

    async def _get(self, request: Request) -> Response:
        if self._foreign(request):
            return Response("Forbidden", status_code=403)
        return self._form()

    async def _post_login(self, request: Request) -> Response:
        if self._foreign(request):
            return Response("Forbidden", status_code=403)
        form = await request.form()
        login = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not login or not password:
            return self._form("Введите логин и пароль.", login)
        self._attempts += 1
        if self._attempts > 8:
            return self._form("Слишком много попыток. Вызовите login заново.", login)
        if self._auth:
            with contextlib.suppress(OSError, ValueError):
                write_diagnostic(self._auth.diagnostics, self.store)
            with contextlib.suppress(Exception):
                await self._auth.close()
        self._auth = MireaAuth()
        self._login = login
        self._otp_attempts = 0
        self._auth.checkpoint_handler = lambda cookies: self.store.save_candidate(cookies, login=self._login)
        try:
            result = await self._auth.login(login, password)
        except Exception:
            logger.warning("login failed; raw error details suppressed")
            return self._form("МИРЭА не ответила. Попробуйте позже.", login)
        if result.challenge:
            self._challenge = result.challenge
            return self._otp()
        if result.cookies:
            return await self._finish(result.cookies)
        if not result.success:
            return self._form(result.message or "Не удалось войти.", login)
        return self._form("SSO не вернула кандидата сессии.", login)

    async def _post_otp(self, request: Request) -> Response:
        if self._foreign(request):
            return Response("Forbidden", status_code=403)
        if not self._auth or not self._challenge:
            return self._form("Сначала введите логин и пароль.")
        form = await request.form()
        code = str(form.get("code", "")).strip()
        self._otp_attempts += 1
        if self._otp_attempts > 3:
            self._challenge = None
            return self._form("Лимит подтверждений исчерпан; новые коды не отправлялись.")
        try:
            result = await self._auth.complete_2fa(self._challenge, code)
        except Exception:
            logger.warning("otp flow stopped; raw error details suppressed")
            self._challenge = None
            return self._form("Подтверждение остановлено. Сохранённого кандидата можно проверить отдельно.")
        if result.challenge:
            self._challenge = result.challenge
            return self._otp(result.message or "Неверный код.")
        self._challenge = None
        if result.cookies:
            return await self._finish(result.cookies)
        return self._form(result.message or "Подтверждение остановлено; нового запроса кода нет.")


def build_local(store: SessionStore | None = None) -> tuple[MCPServer, LocalLogin]:
    store = store if store is not None else LocalSessionStore()
    store.configure()
    mcp = MCPServer(name="mirea-connect", title="MIREA Connect", instructions=INSTRUCTIONS)
    source = LocalSource(store)
    tool = register_tools(mcp, source)
    page = LocalLogin(store)

    @tool(ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def verify_login(candidate_id: str | None = None) -> dict:
        """Проверить сохранённого кандидата без пароля и нового кода. Без ID — последний кандидат.
        Только чтение Pulse/СДО и новые зашифрованные версии; ничего не удаляет."""
        try:
            answer = await verify_candidate(store, candidate_id)
            try:
                write_diagnostic([{"phase": "verify", "checkpoint_saved": True}], store)
            except (OSError, ValueError):
                answer["warnings"].append("diagnostic_save_failed")
            return answer
        except (NotLoggedIn, ValueError, OSError):
            raise S.ServiceError("Сохранённый кандидат недоступен; существующие файлы не изменены.") from None

    @tool(LOCAL_ACTION)
    async def export_materials(course: str | None = None) -> dict:
        """Экспорт опубликованных материалов СДО в локальный ZIP. Без course — все доступные
        курсы; course — название курса или его однозначная часть, например «Большие данные».
        ID знать не нужно. Неоднозначное название вернёт варианты, не выберет курс наугад.
        Возвращает пути к архиву/манифесту, статус и счётчики.
        Не запускает тесты и не отправляет работы. Неполный экспорт отмечается явно."""
        from mcp.server.mcpserver.exceptions import ToolError

        from .material_commands import export_to_disk

        async with source.use() as cookies:
            try:
                return await export_to_disk(cookies, course)
            except OSError as exc:
                raise ToolError(f"Не удалось сохранить экспорт: {type(exc).__name__}") from None

    @tool(LOCAL_ACTION)
    async def login(open_browser: bool = True) -> dict:
        """Открыть страницу входа в МИРЭА на этом компьютере (127.0.0.1). Пароль вводится в браузере,
        не в чате. Верни пользователю ссылку из ответа; после входа личные инструменты заработают."""
        url = await page.start()
        opened = False
        if open_browser:
            with contextlib.suppress(Exception):
                opened = webbrowser.open(url)
        return {
            "url": url,
            "browser_opened": opened,
            "message": "Откройте ссылку и войдите в МИРЭА. Страница работает 15 минут и только на этом компьютере.",
        }

    return mcp, page


def run_stdio(store: SessionStore | None = None) -> None:
    logging.basicConfig(level=logging.WARNING)  # stderr; stdout занят протоколом MCP
    mcp, _ = build_local(store)
    mcp.run("stdio")
