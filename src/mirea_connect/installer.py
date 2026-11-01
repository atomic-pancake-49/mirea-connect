"""Регистрация локального коннектора в MCP-клиентах: Claude Desktop, Claude Code, Cursor,
Codex, Hermes Agent, OpenClaw."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "mirea"


def server_command() -> list[str]:
    """Абсолютная команда запуска: GUI-клиенты не видят PATH из профиля shell."""
    return [sys.executable, "-m", "mirea_connect", "local"]


INSTALL_DOC = "https://raw.githubusercontent.com/atomic-pancake-49/mirea-connect/main/llms-install.md"


def server_spec() -> dict:
    cmd = server_command()
    return {"name": NAME, "command": cmd[0], "args": cmd[1:], "transport": "stdio"}


def agent_prompt() -> str:
    """Промпт для любого агента/харнесса, который не поддержан установщиком напрямую."""
    spec = server_spec()
    args = json.dumps(spec["args"], ensure_ascii=False)
    return f"""Подключи к себе MCP-сервер MIREA Connect (РТУ МИРЭА: расписание, БРС, посещаемость, турникеты).

Он уже установлен на этом компьютере. Параметры:
- имя: {spec["name"]}
- транспорт: stdio
- command: {spec["command"]}
- args: {args}
- переменные окружения и рабочий каталог не нужны

Сделай так:
1. Добавь этот сервер в свою конфигурацию MCP — через свою команду добавления MCP-сервера, если она есть, иначе правкой своего конфига (сначала сохрани копию файла). Используй именно абсолютный путь из command.
2. Перезагрузи MCP-серверы и проверь, что появились инструменты timetable, my_grades, login; вызови whoami.
3. Для входа в МИРЭА вызови инструмент login и дай мне ссылку на страницу 127.0.0.1. Никогда не спрашивай мой пароль или код МИРЭА в чате.

Полная инструкция: {INSTALL_DOC}"""


def claude_desktop_config() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude" / "claude_desktop_config.json"


def cursor_config() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def hermes_config() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "config.yaml"


def openclaw_config() -> Path:
    return Path(os.environ.get("OPENCLAW_HOME") or Path.home() / ".openclaw") / "openclaw.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _q(value: str) -> str:
    """Строка в двойных кавычках — валидна и в TOML, и в YAML."""
    return json.dumps(value, ensure_ascii=False)


# --- Codex: ~/.codex/config.toml, таблица [mcp_servers.mirea] ---


def edit_codex_toml(text: str, add: bool) -> tuple[str, bool]:
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    removed = False
    header = f"[mcp_servers.{NAME}"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped == f"{header}]" or stripped.startswith(f"{header}.")
            removed = removed or skipping
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if add:
        cmd = server_command()
        if out:
            out.append("")
        out.append(f"{header}]")
        out.append(f"command = {_q(cmd[0])}")
        out.append("args = [" + ", ".join(_q(a) for a in cmd[1:]) + "]")
    changed = add or removed
    return ("\n".join(out) + "\n") if out else "", changed


def _codex(add: bool) -> bool:
    path = codex_config()
    codex = shutil.which("codex")
    if codex:
        subprocess.run([codex, "mcp", "remove", NAME], capture_output=True)
        if not add:
            return True
        r = subprocess.run([codex, "mcp", "add", NAME, "--", *server_command()], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        print(f"  ! Codex CLI: {(r.stderr or r.stdout).strip()} — правлю {path} напрямую")
    if not path.parent.exists():
        return False
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    new, changed = edit_codex_toml(text, add)
    if changed:
        _atomic_write(path, new)
    return changed


# --- Hermes Agent: ~/.hermes/config.yaml, ключ mcp_servers ---


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def edit_hermes_yaml(text: str, add: bool) -> tuple[str, bool]:
    lines = text.splitlines()
    top = next((i for i, l in enumerate(lines) if re.match(r"^mcp_servers\s*:", l)), None)
    cmd = server_command()

    def block(indent: int) -> list[str]:
        pad = " " * indent
        return [
            f"{pad}{NAME}:",
            f"{pad}  command: {_q(cmd[0])}",
            f"{pad}  args: [" + ", ".join(_q(a) for a in cmd[1:]) + "]",
        ]

    if top is None:
        if not add:
            return text, False
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines += ["mcp_servers:", *block(2)]
        return "\n".join(lines) + "\n", True

    # mcp_servers: {} или другое значение в той же строке — заменяем на блок
    rest = lines[top].split(":", 1)[1].strip()
    if rest and not rest.startswith("#"):
        if rest in ("{}", "null", "~"):
            if not add:
                return text, False
            lines[top] = "mcp_servers:"
            lines[top + 1 : top + 1] = block(2)
            return "\n".join(lines) + "\n", True
        raise ValueError("mcp_servers записан в одну строку — добавьте коннектор вручную")

    end = top + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > 0 or lines[end].lstrip().startswith("#")):
        end += 1
    children = [i for i in range(top + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    child_indent = min((_indent(lines[i]) for i in children), default=2)

    removed = False
    i = top + 1
    while i < end:
        l = lines[i]
        if _indent(l) == child_indent and re.match(rf"^\s*{NAME}\s*:", l):
            j = i + 1
            while j < end and (not lines[j].strip() or _indent(lines[j]) > child_indent):
                j += 1
            del lines[i:j]
            end -= j - i
            removed = True
            continue
        i += 1
    if add:
        lines[top + 1 : top + 1] = block(child_indent)
    return "\n".join(lines) + "\n", add or removed


def _hermes(add: bool) -> bool:
    path = hermes_config()
    if not path.parent.exists():
        return False
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        new, changed = edit_hermes_yaml(text, add)
    except ValueError as e:
        print(f"  ! Hermes: {e} ({path})")
        return False
    if changed:
        _atomic_write(path, new)
        if add:
            print("  i Hermes: выполните /reload-mcp в открытой сессии")
    return changed


# --- OpenClaw: openclaw mcp set/unset (конфиг JSON5, сами не правим) ---


def _openclaw(add: bool) -> bool:
    exe = shutil.which("openclaw")
    cmd = server_command()
    payload = json.dumps({"command": cmd[0], "args": cmd[1:]}, ensure_ascii=False)
    if not exe:
        if openclaw_config().exists() and add:
            print(f"  ! OpenClaw найден, но команды openclaw нет в PATH. Выполните:\n    openclaw mcp set {NAME} '{payload}'")
        return False
    if not add:
        r = subprocess.run([exe, "mcp", "unset", NAME], capture_output=True, text=True)
        return r.returncode == 0
    r = subprocess.run([exe, "mcp", "set", NAME, payload], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! OpenClaw: {(r.stderr or r.stdout).strip()}")
        return False
    return True


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

    for name, fn in (("Codex", _codex), ("Hermes Agent", _hermes), ("OpenClaw", _openclaw)):
        try:
            if fn(add):
                done.append(name)
        except Exception as e:  # один сломанный клиент не должен ронять установку остальных
            print(f"  ! {name}: {e}")

    if done:
        print(f"{verb}:")
        for d in done:
            print(f"  ✓ {d}")
    else:
        print("Не нашёл ни одного поддерживаемого клиента (Claude Desktop, Claude Code, Cursor, Codex, Hermes, OpenClaw).")
        if add:
            print("\nЕсли у вас другой агент — вставьте ему этот промпт, он подключит коннектор сам:\n")
            print(agent_prompt())
    if add:
        print("\nПерезапустите открытые клиенты (Claude Desktop, Cursor, Codex и т.д.), чтобы они подхватили коннектор.")
        print("Войти в МИРЭА: попросите ассистента «войди в МИРЭА» или выполните `mirea-connect login`.")
    return 0
