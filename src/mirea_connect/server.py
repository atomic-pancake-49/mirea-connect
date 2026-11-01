"""Сборка MCP-сервера: OAuth + страницы входа + инструменты."""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pymirea import Config, configure

from . import services as S
from .config import Settings
from .db import Database
from .oauth import SCOPE, MireaOAuthProvider, is_guest
from .sessions import NoSession, SessionVault
from .web import LoginFlow

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Коннектор к РТУ МИРЭА: Пульс (личное расписание, баллы БРС, посещаемость, турникеты) "
    "и публичное расписание групп, преподавателей и аудиторий. Время московское (UTC+3). "
    "Личные инструменты (my_*) требуют входа в МИРЭА при подключении коннектора; если сессии нет "
    "или она истекла, попроси пользователя переподключить коннектор (отключить и подключить заново) "
    "и войти — никогда не спрашивай пароль или код в чате. "
    "Для расписания своей группы, если пользователь не вошёл, используй timetable с названием группы."
)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

RELOGIN_HINT = (
    "Отключите и заново подключите коннектор MIREA в настройках клиента и войдите в МИРЭА — "
    "пароль в чат вводить не нужно."
)


def _current_user() -> str | None:
    token = get_access_token()
    return token.subject if token else None


def _guard(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except NoSession as e:
            raise ToolError(f"{e} {RELOGIN_HINT}") from e
        except S.SessionExpired as e:
            raise ToolError(f"МИРЭА отклонила сессию ({e}). {RELOGIN_HINT}") from e
        except S.ServiceError as e:
            raise ToolError(str(e)) from e

    return wrapper


def build(settings: Settings) -> tuple[MCPServer, Database]:
    configure(Config(session_keys=settings.resolve_session_keys(), mirea_proxy=settings.mirea_proxy))
    db = Database(settings.data_dir / "mirea-connect.sqlite3")
    provider = MireaOAuthProvider(db, settings)
    vault = SessionVault(db, settings.session_keys)
    flow = LoginFlow(provider, vault, settings)

    mcp = MCPServer(
        name="mirea-connect",
        title="MIREA Connect",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            # Токены выдаём сами и только для этого сервера; часть клиентов не передаёт resource.
            validate_token_resource=False,
        ),
    )

    mcp.custom_route("/", methods=["GET"])(flow.index)
    mcp.custom_route("/health", methods=["GET"])(flow.health)
    mcp.custom_route("/login", methods=["GET"])(flow.get_login)
    mcp.custom_route("/login", methods=["POST"])(flow.post_login)
    mcp.custom_route("/login/otp", methods=["POST"])(flow.post_otp)

    def tool(annotations: ToolAnnotations = READ):
        def deco(fn):
            return mcp.tool(annotations=annotations)(_guard(fn))

        return deco

    async def _with_session(op: Callable[[dict], Awaitable[Any]]) -> Any:
        user_id = _current_user()
        if is_guest(user_id):
            raise ToolError("Вы подключились без входа в МИРЭА — доступно только публичное расписание. " + RELOGIN_HINT)
        async with vault.use(user_id) as cookies:
            try:
                return await op(cookies)
            except S.SessionExpired:
                db.update_session(user_id, None)
                raise

    # --- аккаунт ---

    @tool()
    async def whoami() -> dict:
        """Кто подключён к коннектору: логин МИРЭА или гостевой режим, есть ли живая сессия Пульса."""
        user_id = _current_user()
        if is_guest(user_id):
            return {"mode": "guest", "personal_tools": False}
        info = vault.info(user_id) or {}
        return {
            "mode": "mirea",
            "login": info.get("login"),
            "personal_tools": bool(info.get("session_blob")),
        }

    @tool(DESTRUCTIVE)
    async def delete_my_data() -> dict:
        """Удалить с сервера сохранённую сессию МИРЭА пользователя и отозвать все его токены.
        После этого коннектор нужно подключить заново. Вызывай только по явной просьбе пользователя."""
        user_id = _current_user()
        if user_id and not is_guest(user_id):
            vault.forget(user_id)
        elif user_id:
            db.delete_user_tokens(user_id)
        return {"deleted": True}

    # --- личное ---

    @tool()
    async def my_schedule(days: int = 7) -> dict:
        """Личное расписание из Пульса на ближайшие `days` дней (1–31), начиная с сегодня."""
        return await _with_session(lambda c: S.my_schedule(c, days))

    @tool()
    async def my_grades() -> dict:
        """Баллы БРС за текущий семестр по всем предметам: текущий контроль (из 40), семестровый (из 30),
        посещения (из 30), достижения, дополнительные, итог (из 120). attendance_max_possible —
        достижимый максимум за посещения, если известен."""
        return await _with_session(S.grades)

    @tool()
    async def my_attendance(discipline: str) -> dict:
        """Посещаемость по одной дисциплине: сводка и статус каждого занятия.
        `discipline` — discipline_id из my_grades или часть названия предмета."""
        return await _with_session(lambda c: S.attendance(c, discipline))

    @tool()
    async def my_turnstile_events() -> dict:
        """Проходы через турникеты (СКУД) за сегодня: время, зоны, длительность пребывания."""
        return await _with_session(S.acs_today)

    # --- публичное ---

    @tool()
    async def search_timetable(kind: str, query: str) -> list[dict]:
        """Поиск в публичном расписании. kind: group | teacher | classroom. Возвращает совпадения с uid."""
        return await S.search(kind, query)

    @tool()
    async def timetable(kind: str, query: str, days: int = 7, date_from: str | None = None) -> dict:
        """Публичное расписание группы, преподавателя или аудитории (вход не нужен).
        kind: group | teacher | classroom; query — название («ЭФБО-01-24», «Иванов И.И.», «А-101»).
        days — сколько дней показать, начиная с date_from (YYYY-MM-DD, по умолчанию сегодня); 0 — всё."""
        return await S.timetable(kind, query, days, date_from)

    @tool()
    async def free_rooms(rooms: list[str], start_time: str, end_time: str, date: str | None = None) -> dict:
        """Проверить, свободны ли аудитории в интервал времени (по публичному расписанию).
        rooms — до 20 названий («А-101», «ИВЦ-124»); start_time/end_time — HH:MM; date — YYYY-MM-DD, по умолчанию сегодня."""
        return await S.free_rooms(rooms, date, start_time, end_time)

    return mcp, db


def make_app(settings: Settings):
    mcp, _ = build(settings)
    allowed = sorted(
        {settings.public_host, "localhost", "127.0.0.1", f"localhost:{settings.port}", f"127.0.0.1:{settings.port}"}
        | set(settings.extra_allowed_hosts)
    )
    return mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=[settings.public_url, "https://claude.ai", "https://claude.com"],
        ),
        host=settings.host,
    )


def main() -> None:
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="mirea-connect", description="MCP-коннектор к РТУ МИРЭА")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--public-url", help="внешний адрес, например https://mirea.example.com")
    args = p.parse_args()

    settings = Settings.from_env()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.public_url:
        settings.public_url = args.public_url.rstrip("/")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет полные URL запросов к sso.mirea.ru — там бывают одноразовые коды.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info("MIREA Connect: %s (слушаю %s:%s)", settings.mcp_url, settings.host, settings.port)
    uvicorn.run(make_app(settings), host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
