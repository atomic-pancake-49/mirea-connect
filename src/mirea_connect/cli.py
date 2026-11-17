"""Точка входа ``mirea-connect``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from . import __version__
from . import services as S


def _force_utf8() -> None:
    """Кириллица в консоли: без этого старая консоль Windows (cp866/cp1251) выдаёт
    кракозябры, а при stdout=ascii `print` вообще падает. На 3.7+ переводим потоки в UTF-8."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _write_auth_diagnostic(auth, store=None):
    from .login_session import write_diagnostic

    path = write_diagnostic(getattr(auth, "diagnostics", []), store)
    print(f"Диагностика без пароля, кода и cookies: {path}")
    return path


def _store():
    from .session_state import LocalSessionStore

    store = LocalSessionStore()
    store.configure()
    return store


def _show_verification(answer):
    pulse, sdo = answer["pulse"], answer["sdo"]
    if answer["status"] == "verified":
        if "journals_count" in pulse:
            print(f"Сессия Pulse сохранена и проверена; личных журналов: {pulse['journals_count']}.")
        else:
            print(f"Сессия Pulse сохранена и проверена; предметов: {pulse.get('subjects_count', 0)}.")
    else:
        print("Кандидат сохранён зашифрованно, но доступ Pulse пока не подтверждён.")
    if sdo["status"] == "verified":
        print(f"СДО проверена; курсов: {sdo.get('courses_count', 0)}.")
    else:
        print("Доступ СДО пока не подтверждён; это отдельная от Pulse проверка.")
    print(f"Повторная проверка без пароля и нового кода: mirea-connect verify-login {answer['candidate_id']}")
    return 0 if answer["status"] == "verified" and sdo["status"] == "verified" else 2


async def _login_terminal(login: str | None) -> int:
    from .auth import MireaAuth
    from .login_session import verify_candidate

    store = _store()
    login = login or os.environ.get("MIREA_LOGIN") or input("Логин (…@edu.mirea.ru): ").strip()
    password = getpass.getpass("Пароль (не отображается): ")
    auth = MireaAuth()
    auth.checkpoint_handler = lambda cookies: store.save_candidate(cookies, login=login)
    result, answer = None, None
    try:
        result = await auth.login(login, password)
        attempts, limit = 0, 3
        while result.challenge and attempts < limit:
            kind = result.challenge.kind
            prompt = "Код из последнего письма" if kind == "email_code" else ("Код из MAX" if kind == "max_otp" else "Код из приложения-аутентификатора")
            code = getpass.getpass(prompt + " (не отображается): ").strip()
            attempts += 1
            result = await auth.complete_2fa(result.challenge, code)
            if result.challenge:
                print("SSO всё ещё запрашивает подтверждение; следующая отправка ограничена общим лимитом.")
        if not result.challenge and not getattr(auth, "checkpoint_error", False):
            ident = getattr(auth, "candidate_id", None)
            if not ident and result.cookies:
                ident = store.save_candidate(result.cookies, login=login)
            if ident:
                answer = await verify_candidate(store, ident)
    finally:
        try:
            _write_auth_diagnostic(auth, store)
        except (OSError, ValueError):
            print("Не удалось сохранить безопасную диагностику; секреты не выводились.", file=sys.stderr)
        await auth.close()
    if answer is not None:
        return _show_verification(answer)
    if result and result.challenge:
        print("Лимит подтверждений исчерпан; дополнительные коды не отправлялись.", file=sys.stderr)
    elif getattr(auth, "checkpoint_error", False):
        print("Кандидат не удалось записать. Вход не объявлен успешным.", file=sys.stderr)
    else:
        print(f"Вход не подтверждён: {result.message if result else 'процесс остановлен'}", file=sys.stderr)
    return 1


async def _verify_login_command(candidate_id=None, *, use_json=False):
    import json

    from pymirea.store import NotLoggedIn

    from .login_session import verify_candidate, write_diagnostic

    store = _store()
    try:
        answer = await verify_candidate(store, candidate_id)
    except (NotLoggedIn, ValueError, OSError):
        if use_json:
            print(json.dumps({"status": "not_found", "reason": "candidate_unavailable"}))
        else:
            print("Кандидат не найден или недоступен; существующие файлы сохранены.", file=sys.stderr)
        return 1
    try:
        write_diagnostic([{"phase": "verify", "checkpoint_saved": True}], store)
    except (OSError, ValueError):
        answer["warnings"].append("diagnostic_save_failed")
    if use_json:
        print(json.dumps(answer, ensure_ascii=False, indent=2))
        return 0 if answer["status"] == "verified" and answer["sdo"]["status"] == "verified" else 2
    return _show_verification(answer)


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mirea-connect", description="MCP-коннектор к РТУ МИРЭА")
    p.add_argument("--version", action="version", version=f"mirea-connect {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="КОМАНДА")

    s = sub.add_parser("install", help="подключить коннектор к клиентам (меню выбора)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="без вопросов, ко всем найденным клиентам")
    g.add_argument("--prompt", action="store_true", help="только показать промпт для другого агента")
    sub.add_parser("uninstall", help="отключить коннектор от клиентов")
    s = sub.add_parser("prompt", help="промпт для любого другого агента, чтобы он подключил коннектор сам")
    s.add_argument("--json", action="store_true", help="только параметры сервера в JSON")
    s = sub.add_parser("login", help="войти в МИРЭА в терминале")
    s.add_argument("login", nargs="?")
    s = sub.add_parser("verify-login", help="проверить сохранённого кандидата без пароля и нового кода")
    s.add_argument("candidate_id", nargs="?")
    s.add_argument("--json", action="store_true")
    sub.add_parser("logout", help="показать политику сохранения сессий (удаление отключено)")
    sub.add_parser("status", help="есть ли вход")
    sub.add_parser("local", help="локальный MCP-сервер (stdio) — его запускает клиент")
    s = sub.add_parser("materials", help="экспорт материалов СДО в локальный ZIP")
    scope = s.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="все доступные курсы")
    scope.add_argument("--course", help="название курса или его однозначная часть")
    s.add_argument("--json", action="store_true", help="результат в JSON")
    s = sub.add_parser("serve", help="удалённый сервер с OAuth (для self-host)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--public-url")

    from .commands import DATA_COMMANDS, add_subparsers

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="вывести результат в JSON")
    add_subparsers(sub, common)

    args = p.parse_args(argv)
    if args.cmd != "local":  # у local stdout — канал MCP-протокола, перекодировать нельзя
        _force_utf8()
    if args.cmd in DATA_COMMANDS:
        from .commands import run as run_data

        try:
            return asyncio.run(run_data(args, _store()))
        except S.ServiceError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
        except Exception:
            # Never print raw upstream exception text or a traceback with URLs.
            print("Ошибка: не удалось получить данные МИРЭА. Попробуйте позже.", file=sys.stderr)
            return 1
    try:
        if args.cmd == "install":
            from .installer import install

            return install(True, mode="all" if args.all else "prompt" if args.prompt else "auto")
        if args.cmd == "prompt":
            import json

            from .installer import agent_prompt, server_spec

            print(json.dumps(server_spec(), ensure_ascii=False) if args.json else agent_prompt())
            return 0
        if args.cmd == "uninstall":
            from .installer import install

            return install(False)
        if args.cmd == "login":
            return asyncio.run(_login_terminal(args.login))
        if args.cmd == "verify-login":
            return asyncio.run(_verify_login_command(args.candidate_id, use_json=args.json))
        if args.cmd == "logout":
            print("Удаление отключено. Все версии сессий сохранены; новые входы не заменяют старые файлы.")
            return 1
        if args.cmd == "status":
            from pymirea.store import NotLoggedIn

            try:
                s = _store().load()
                print(f"Сохранена сессия: {s.login}. Текущий доступ проверяется личным запросом, не этой командой.")
                return 0
            except NotLoggedIn:
                print("Вход не выполнен. `mirea-connect login`")
                return 1
        if args.cmd == "local":
            from .local import run_stdio

            run_stdio()
            return 0
        if args.cmd == "materials":
            import json

            from .material_commands import export_for_store
            from .sessions import NoSession

            try:
                result = asyncio.run(export_for_store(_store(), args.course))
            except (S.ServiceError, NoSession) as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
                return 1
            except OSError as exc:
                print(f"Не удалось сохранить экспорт: {type(exc).__name__}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{result['status']}: {result['archive']}")
                for name in result.get("course_names", []):
                    print(f"  {name}")
                print(f"Курсов: {result['courses']}; файлов: {result['saved_files']}; манифест: {result['manifest']}")
                if result.get("message"):
                    print(result["message"])
            return 0 if result["status"] == "complete" else 2
        if args.cmd == "serve":
            from .server import serve

            serve(args.host, args.port, args.public_url)
            return 0
    except KeyboardInterrupt:
        return 130
    p.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Final local-command boundary, including upstream/client-close failures."""
    try:
        return _main(argv)
    except S.ServiceError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Ошибка: операция МИРЭА не завершена. Секретные детали не выводились.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
