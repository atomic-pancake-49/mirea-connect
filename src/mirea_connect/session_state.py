"""Append-only encrypted local session candidates; legacy files are read-only."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pymirea.crypto import SessionCrypto
from pymirea.store import NotLoggedIn, SessionStore, StoredSession

_ID = re.compile(r"[0-9a-f]{32}\Z")
_RECORD = re.compile(r"record-(\d{20})-([0-9a-f]{32})\.json\Z")
_MAX_RECORD = 4 * 1024 * 1024


class OperationCookies(dict):
    """Ephemeral read transaction; normal return alone never authorizes a save."""
    persist_allowed = False


def approve_session_updates(cookies: dict) -> None:
    """Called only after an operation positively confirms its account reads."""
    if isinstance(cookies, OperationCookies):
        cookies.persist_allowed = True


@dataclass
class CandidateSession:
    candidate_id: str
    cookies: dict
    login: str | None
    saved_at: float


def _no_symlink(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlinked session paths are not permitted")


def _new_private(path: Path, data: bytes) -> None:
    _no_symlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read_private(path: Path) -> bytes:
    _no_symlink(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(_MAX_RECORD + 1)
    if len(data) > _MAX_RECORD:
        raise ValueError("Session record exceeds the size limit")
    return data


class LocalSessionStore(SessionStore):
    """Compatible loader plus immutable candidate/verified history. Never clear."""

    def __init__(self, home=None):
        super().__init__(home)
        self.home = self.home.absolute()
        self.key_path = self.home / "key"
        self.session_path = self.home / "session.json"  # legacy, never replaced
        self.records_path = self.home / "retained"
        _no_symlink(self.home)

    def _ensure_home(self):
        _no_symlink(self.home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)

    def configure(self):
        # Some upstream warning paths include raw authorization URLs. Surface
        # only our structured outcomes instead, in this local connector process.
        logging.getLogger("pymirea").setLevel(logging.CRITICAL)
        super().configure()

    def session_keys(self):
        _no_symlink(self.key_path)
        configured = os.environ.get("PYMIREA_SESSION_KEYS")
        if configured:
            return configured
        if self.key_path.exists():
            key = _read_private(self.key_path).decode("utf-8").strip()
            if not key:
                raise NotLoggedIn("Ключ сессии повреждён; существующие файлы сохранены.")
            return key
        self._ensure_home()
        key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        try:
            _new_private(self.key_path, (key + "\n").encode("ascii"))
        except FileExistsError:
            return _read_private(self.key_path).decode("utf-8").strip()
        return key

    def _cipher(self):
        return SessionCrypto(self.session_keys())

    def _paths(self):
        _no_symlink(self.records_path)
        if not self.records_path.exists():
            return []
        paths = []
        for path in self.records_path.iterdir():
            if _RECORD.fullmatch(path.name):
                _no_symlink(path)
                if path.is_file():
                    paths.append(path)
        return sorted(paths, key=lambda p: p.name, reverse=True)

    def _decode(self, path):
        match = _RECORD.fullmatch(path.name)
        wrapper = json.loads(_read_private(path))
        if not isinstance(wrapper, dict) or wrapper.get("schema") != 1 or not isinstance(wrapper.get("blob"), str):
            raise ValueError("Invalid session wrapper")
        payload = self._cipher().decrypt_session(wrapper.get("blob", ""))
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise ValueError("Undecryptable session record")
        if payload.get("kind") not in {"candidate", "verified"} or payload.get("id") != match[2]:
            raise ValueError("Invalid session identity")
        if payload.get("saved_ns") != int(match[1]) or not isinstance(payload.get("cookies"), dict) or not payload["cookies"]:
            raise ValueError("Invalid session payload")
        if payload.get("login") is not None and not isinstance(payload["login"], str):
            raise ValueError("Invalid login metadata")
        return payload

    def _records(self, kind):
        for path in self._paths():
            try:
                payload = self._decode(path)
            except (ValueError, OSError, TypeError, UnicodeError):
                continue  # incomplete records are retained, not cleaned up
            if payload["kind"] == kind:
                yield payload

    def _append(self, kind, cookies, login=None, source_candidate=None):
        if not isinstance(cookies, dict) or not cookies:
            raise ValueError("An empty session cannot be retained")
        self._ensure_home()
        _no_symlink(self.records_path)
        self.records_path.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.records_path, 0o700)
        ident, stamp = uuid4().hex, time.time_ns()
        payload = {"schema": 1, "kind": kind, "id": ident, "saved_ns": stamp,
                   "source_candidate": source_candidate, "cookies": cookies, "login": login}
        wrapper = {"schema": 1, "blob": self._cipher().encrypt_session(payload)}
        path = self.records_path / f"record-{stamp:020d}-{ident}.json"
        _new_private(path, json.dumps(wrapper, separators=(",", ":")).encode("utf-8"))
        return ident

    def save_candidate(self, cookies, *, login=None):
        return self._append("candidate", cookies, login)

    def load_candidate(self, candidate_id=None):
        if candidate_id is not None and (not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id)):
            raise ValueError("Укажите идентификатор кандидата, не путь к файлу.")
        for record in self._records("candidate"):
            if candidate_id is None or record["id"] == candidate_id:
                return CandidateSession(record["id"], record["cookies"], record.get("login"), record["saved_ns"] / 1e9)
        raise NotLoggedIn("Сохранённый кандидат входа не найден; существующие файлы не изменены.")

    def mark_verified(self, candidate_id, cookies=None):
        candidate = self.load_candidate(candidate_id)
        return self._append("verified", cookies if cookies is not None else candidate.cookies,
                            candidate.login, source_candidate=candidate.candidate_id)

    def load(self):
        for record in self._records("verified"):
            return StoredSession(record.get("login"), record["saved_ns"] / 1e9, record["cookies"])
        _no_symlink(self.session_path)
        return super().load()

    def save(self, cookies, *, login=None):
        if login is None:
            try:
                login = self.load().login
            except NotLoggedIn:
                pass
        return self._append("verified", cookies, login)

    def exists(self):
        try:
            self.load()
            return True
        except NotLoggedIn:
            return False

    def clear(self):
        raise RuntimeError("Удаление сессии отключено: все сохранённые версии остаются на диске.")
