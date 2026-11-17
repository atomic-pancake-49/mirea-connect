"""Shared candidate verification and secret-free diagnostics for CLI/local MCP."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from pymirea.store import default_home

from . import services as S
from .portal_clients import MireaGrades, MireaSDO
from .session_state import _new_private, _no_symlink

_HOSTS = {"sso.mirea.ru", "pulse.mirea.ru", "online-edu.mirea.ru"}
_PATHS = {"/", "/api/baseinfo", "/api/auth/login", "/api/mireaauth",
          "/realms/mirea/protocol/openid-connect/auth",
          "/realms/mirea/login-actions/authenticate", "/realms/mirea/login-actions/otp",
          "/realms/mirea/login-actions/required-action"}
_FIELDS = {"username", "password", "emailCode", "code", "otp", "login", "skip", "state",
           "session_state", "iss", "id_token", "selectedCredentialId"}
_FLAGS = {"showSkip", "isEnrollment", "phoneNotRegistered", "noPhoneNumber", "deliveryPending", "sendFailed"}
_BOOL_FIELDS = {"server_error_present", "submit_login", "callback", "callback_form", "callback_repeated",
                "max_context_parsed", "max_skip_offered", "max_skip_used", "max_skip_action_trusted",
                "max_skip_repeated", "form_parse_skipped", "candidate_available", "checkpoint_saved"}
_PAGE_IDS = {"qr-login", "login", "email-code-form", "login-max-otp", "login-otp", "required-action"}
_COOKIE_CLASSES = {"aspnet_auth", "aspnet_auth_chunk", "correlation", "nonce", "keycloak", "moodle", "other"}


def safe_path(value):
    return value if value in _PATHS else "other"


def safe_diagnostics(rows):
    """Project only known shapes. Never serialize arbitrary message/URL/value fields."""
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out = {}
        for key in _BOOL_FIELDS:
            if type(row.get(key)) is bool:
                out[key] = row[key]
        for key in ("status", "set_cookie_count", "form_count"):
            if type(row.get(key)) is int:
                out[key] = row[key]
        if row.get("phase") in {"password", "otp", "skip", "callback", "response", "verify"}:
            out["phase"] = row["phase"]
        if row.get("event") == "http_hop":
            out["event"] = "http_hop"
        if "host" in row:
            out["host"] = row["host"] if row["host"] in _HOSTS else "other"
        if "path" in row:
            out["path"] = safe_path(row["path"])
        if row.get("method") in {"GET", "POST"}:
            out["method"] = row["method"]
        if "page_id" in row:
            out["page_id"] = row["page_id"] if row["page_id"] in _PAGE_IDS else None
        if isinstance(row.get("submitted_fields"), list):
            out["submitted_fields"] = sorted({x for x in row["submitted_fields"] if isinstance(x, str) and x in _FIELDS})
        if row.get("challenge_kind") in {"email_code", "max_otp", "otp"}:
            out["challenge_kind"] = row["challenge_kind"]
        if row.get("code_field") in {"emailCode", "code", "otp"}:
            out["code_field"] = row["code_field"]
        if isinstance(row.get("max_flags"), dict):
            out["max_flags"] = {k: v for k, v in row["max_flags"].items() if k in _FLAGS and type(v) is bool}
        if isinstance(row.get("cookies"), list):
            out["cookies"] = []
            for cookie in row["cookies"][:64]:
                if not isinstance(cookie, dict):
                    continue
                out["cookies"].append({
                    "class": cookie.get("class") if cookie.get("class") in _COOKIE_CLASSES else "other",
                    "domain": cookie.get("domain") if cookie.get("domain") in _HOSTS else "other",
                    "path": safe_path(cookie.get("path")),
                    **{k: v for k, v in cookie.items() if k in {"secure", "httponly", "deleted"} and type(v) is bool},
                })
        if isinstance(row.get("forms"), list):
            out["forms"] = [{"method": f.get("method") if f.get("method") in {"post", "get"} else "other",
                             "action": "pulse_callback" if f.get("action") == "pulse_callback" else "other",
                             "known_fields": sorted(set(f.get("known_fields", [])) & _FIELDS),
                             "unknown_field_count": f.get("unknown_field_count") if type(f.get("unknown_field_count")) is int else 0}
                            for f in row["forms"][:4] if isinstance(f, dict)]
        result.append(out)
    return result


def write_diagnostic(rows, store=None):
    home = getattr(store, "home", None) or default_home()
    root = Path(os.environ.get("MIREA_CONNECT_DIAGNOSTICS_DIR") or (Path(home).parent / "auth-diagnostics"))
    _no_symlink(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / (uuid4().hex + ".json")
    _new_private(path, json.dumps(safe_diagnostics(rows), ensure_ascii=False, indent=2).encode("utf-8"))
    return path


async def _sdo_read(cookies):
    api = MireaSDO(session_cookies=cookies)
    try:
        result = await api.get_courses()
    finally:
        await api.close()
    if not result.success:
        raise S.ServiceError("Список курсов СДО пока не подтверждён.")
    return {"courses_count": len(result.courses or [])}


async def _pulse_read(cookies):
    """A real account-specific read proves access, independently of BRS support."""
    api = MireaGrades(session_cookies=cookies)
    try:
        logs, error = await api._get_visiting_logs()
    finally:
        await api.close()
    if error or not logs:
        raise S.ServiceError("Pulse не подтвердил доступ к личным журналам.")
    return {"journals_count": len(logs)}


async def verify_candidate(store, candidate_id=None, *, pulse_reader=None, sdo_reader=None):
    """No authentication prompts or credential POSTs; only existing session reads."""
    candidate = store.load_candidate(candidate_id)
    cookies = dict(candidate.cookies)
    answer = {"status": "candidate", "candidate_id": candidate.candidate_id,
              "pulse": {"status": "unverified"}, "sdo": {"status": "unverified"}, "warnings": []}
    try:
        pulse = await (pulse_reader or _pulse_read)(cookies)
        if not isinstance(pulse, dict):
            raise S.ServiceError("Не подтверждён формат данных Pulse.")
        if type(pulse.get("journals_count")) is int and pulse["journals_count"] > 0:
            answer["pulse"] = {"status": "verified", "journals_count": pulse["journals_count"]}
        elif isinstance(pulse.get("subjects"), list):
            answer["pulse"] = {"status": "verified", "subjects_count": len(pulse["subjects"])}
        else:
            raise S.ServiceError("Не подтверждён формат данных Pulse.")
    except Exception:
        answer["pulse"] = {"status": "unverified", "reason": "pulse_read_failed"}
        cookies = dict(candidate.cookies)
    pulse_cookies = dict(cookies)
    try:
        sdo = await (sdo_reader or _sdo_read)(cookies)
        count = sdo.get("courses_count") if isinstance(sdo, dict) else None
        if count is None and isinstance(sdo, dict) and isinstance(sdo.get("courses"), list):
            count = len(sdo["courses"])
        if type(count) is not int or count < 0:
            raise S.ServiceError("Не подтверждён формат данных СДО.")
        answer["sdo"] = {"status": "verified", "courses_count": count}
    except Exception:
        answer["sdo"] = {"status": "unverified", "reason": "sdo_read_failed"}
    # A failed probe must not supersede the original recovery checkpoint.
    # SDO may update its cache, but cannot replace Pulse's just-verified cookies.
    if answer["sdo"]["status"] == "verified" and cookies.get("__sdo_moodle_session"):
        pulse_cookies["__sdo_moodle_session"] = cookies["__sdo_moodle_session"]
    if answer["pulse"]["status"] == "verified":
        try:
            store.mark_verified(candidate.candidate_id, cookies=pulse_cookies)
            answer["status"] = "verified"
        except (OSError, ValueError):
            answer["warnings"].append("verified_record_save_failed")
    return answer
