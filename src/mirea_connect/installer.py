"""Регистрация локального коннектора в MCP-клиентах: Claude Desktop, Claude Code, Cursor."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "mirea"


def server_command() -> list[str]:
    """Абсолютная команда запуска: GUI-клиенты не видят PATH из профиля shell."""
    return [sys.executable, "-m", "mirea_connect", "local"]


def claude_desktop_config() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def cursor_config() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _merge_json(path: Path, add: bool) -> bool:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
            print(f"  ! {path} не читается как JSON, копия сохранена в {backup}")
            data = {}
    servers = data.setdefault("mcpServers", {})
    if add:
        cmd = server_command()
        servers[NAME] = {"command": cmd[0], "args": cmd[1:]}
    else:
        if NAME not in servers:
            return False
        servers.pop(NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return True


def _claude_code(add: bool) -> bool:
    claude = shutil.which("claude")
    if not claude:
        return False
    subprocess.run([claude, "mcp", "remove", "--scope", "user", NAME], capture_output=True)
    if not add:
        return True
    r = subprocess.run([claude, "mcp", "add", "--scope", "user", NAME, "--", *server_command()], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! Claude Code: {r.stderr.strip() or r.stdout.strip()}")
        return False
    return True


def install(add: bool = True) -> int:
    verb = "Подключено" if add else "Удалено"
    done: list[str] = []

    desktop = claude_desktop_config()
    if desktop.parent.exists() or desktop.exists():
        if _merge_json(desktop, add):
            done.append(f"Claude Desktop ({desktop})")

    if _claude_code(add):
        done.append("Claude Code")

    cursor = cursor_config()
    if cursor.parent.exists():
        if _merge_json(cursor, add):
            done.append(f"Cursor ({cursor})")

    if done:
        print(f"{verb}:")
        for d in done:
            print(f"  ✓ {d}")
    else:
        print("Не нашёл ни Claude Desktop, ни Claude Code, ни Cursor.")
        if add:
            cmd = server_command()
            print("Добавьте MCP-сервер вручную в свой клиент:")
            print(json.dumps({"mcpServers": {NAME: {"command": cmd[0], "args": cmd[1:]}}}, ensure_ascii=False, indent=2))
    if add:
        print("\nПерезапустите Claude Desktop / Cursor, если они открыты.")
        print("Войти в МИРЭА: попросите ассистента «войди в МИРЭА» или выполните `mirea-connect login`.")
    return 0
