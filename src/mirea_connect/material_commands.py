"""Shared CLI/MCP material export: read SDO, write a unique local ZIP."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import services as S
from .materials import CourseRef, export_materials
from .portal_clients import MireaSDO
from .session_state import approve_session_updates

EXPORT_DIR = Path(os.environ.get("MIREA_EXPORT_DIR", str(Path.home() / "mirea-connect-exports")))


def _private_dir(path: Path) -> None:
    """Reject symlinks, including parent components; preserve all existing data."""
    if not path.is_absolute():
        raise S.ServiceError("Каталог экспорта должен быть абсолютным.")
    for current in reversed((path, *path.parents)):
        if current.is_symlink():
            raise S.ServiceError("Символические ссылки в каталоге экспорта запрещены.")
        if current.exists():
            if not current.is_dir():
                raise S.ServiceError("Путь экспорта не является каталогом.")
        else:
            current.mkdir(mode=0o700)


def _new_file(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(body)


def select_courses(courses: list, query: str | int | None) -> list:
    if query is None:
        return courses
    if type(query) not in (str, int) or not str(query).strip():
        raise S.ServiceError("Укажите название курса.")
    def normalize(value):
        return " ".join(str(value).casefold().replace("ё", "е").split())

    text = normalize(query)
    if text.isdigit():
        matches = [c for c in courses if str(c.id) == text]
    else:
        matches = [c for c in courses if normalize(c.name) == text]
        if not matches:
            matches = [c for c in courses if text in normalize(c.name)]
    if len(matches) == 1:
        return matches
    choices = "\n".join(f"- {c.name}" for c in (matches or courses))
    if matches:
        raise S.ServiceError(f"Название «{query}» неоднозначно. Уточните курс:\n{choices}")
    raise S.ServiceError(f"Курс «{query}» не найден. Доступные курсы:\n{choices}")


async def export_to_disk(cookies: dict, course: str | int | None = None) -> dict:
    """None means all enrolled courses. Never submit, delete or overwrite anything."""
    api = MireaSDO(session_cookies=cookies)
    try:
        listing = await api.get_courses()
        if not listing.success:
            raise S.ServiceError("Не удалось прочитать курсы СДО. Проверьте соединение и выполните вход заново.")
        courses = listing.courses or []
        if not courses:
            raise S.ServiceError("СДО не вернула ни одного курса; экспорт не выполнен.")
        selected = select_courses(courses, course)
        course_id = None if course is None else selected[0].id
        refs = [CourseRef(str(c.id), c.name) for c in selected]
        scope = "all" if course_id is None else f"course:{course_id}"
        _private_dir(EXPORT_DIR)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"mirea-materials-{'all' if course_id is None else course_id}-{stamp}-{uuid4().hex[:12]}"
        zip_path = EXPORT_DIR / (name + ".zip")
        manifest_path = EXPORT_DIR / (name + ".json")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        # Do not hold the complete ZIP in RAM. Failed writes leave a uniquely
        # named partial archive in place; never erase or silently retry over it.
        try:
            with os.fdopen(os.open(zip_path, flags, 0o600), "wb") as output:
                result = await export_materials(api.client, refs, scope=scope, output=output)
        except Exception:
            raise S.ServiceError(f"Экспорт остановлен. Уже созданный архив сохранён: {zip_path}") from None
    finally:
        await api.close()

    _new_file(manifest_path, json.dumps(result.manifest, ensure_ascii=False, indent=2).encode())
    answer = {
        "status": result.status,
        "scope": scope,
        "course_names": [c.name for c in selected],
        "archive": str(zip_path),
        "manifest": str(manifest_path),
        **result.manifest["totals"],
    }
    if result.status == "relogin_required":
        answer["message"] = "Сессия СДО истекла. Войдите заново; архив содержит только уже полученные данные."
    elif result.status != "complete":
        answer["message"] = "Выгрузка неполная; причины пропусков перечислены в манифесте."
    else:
        approve_session_updates(cookies)
    return answer


async def export_for_store(store, course: str | int | None = None) -> dict:
    from .local import LocalSource

    source = LocalSource(store, clear_on_expiry=False)
    async with source.use() as cookies:
        return await export_to_disk(cookies, course)
