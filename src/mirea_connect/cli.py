"""Точка входа ``mirea-connect``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from . import __version__


def _store():
    from pymirea.store import SessionStore

    store = SessionStore()
    store.configure()
    return store


async def _login_terminal(login: str | None) -> int:
    from pymirea.auth import MireaAuth

    store = _store()
    login = login or os.environ.get("MIREA_LOGIN") or input("Логин (…@edu.mirea.ru): ").strip()
    password = getpass.getpass("Пароль (не отображается): ")
    auth = MireaAuth()
    try:
        result = await auth.login(login, password)
        while result.challenge:
            code = input("Код из письма: ").strip()
            result = await auth.complete_2fa(result.challenge, code)
            if result.challenge:
                print(result.message or "Неверный код, попробуйте ещё раз.")
    finally:
        await auth.close()
    if not result.success or not result.cookies:
        print(f"Не удалось войти: {result.message}", file=sys.stderr)
        return 1
    store.save(result.cookies, login=login)
    print("Вы вошли. Сессия сохранена на этом компьютере.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mirea-connect", description="MCP-коннектор к РТУ МИРЭА")
    p.add_argument("--version", action="version", version=f"mirea-connect {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="КОМАНДА")

    sub.add_parser("install", help="подключить коннектор ко всем найденным клиентам")
    sub.add_parser("uninstall", help="отключить коннектор от клиентов")
    s = sub.add_parser("prompt", help="промпт для любого другого агента, чтобы он подключил коннектор сам")
    s.add_argument("--json", action="store_true", help="только параметры сервера в JSON")
    s = sub.add_parser("login", help="войти в МИРЭА в терминале")
    s.add_argument("login", nargs="?")
    sub.add_parser("logout", help="удалить сессию с этого компьютера")
    sub.add_parser("status", help="есть ли вход")
    sub.add_parser("local", help="локальный MCP-сервер (stdio) — его запускает клиент")
    s = sub.add_parser("serve", help="удалённый сервер с OAuth (для self-host)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--public-url")

    args = p.parse_args(argv)
    try:
        if args.cmd == "install":
            from .installer import install

            return install(True)
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
        if args.cmd == "logout":
            print("Сессия удалена." if _store().clear() else "Входа и так не было.")
            return 0
        if args.cmd == "status":
            from pymirea.store import NotLoggedIn

            try:
                s = _store().load()
                print(f"Вход выполнен: {s.login}")
                return 0
            except NotLoggedIn:
                print("Вход не выполнен. `mirea-connect login`")
                return 1
        if args.cmd == "local":
            from .local import run_stdio

            run_stdio()
            return 0
        if args.cmd == "serve":
            from .server import serve

            serve(args.host, args.port, args.public_url)
            return 0
    except KeyboardInterrupt:
        return 130
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
