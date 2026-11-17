"""Pulse-owned PAR login flow. Credentials stay on SSO; Pulse handles its callback."""
from __future__ import annotations

import json
import re
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pymirea.auth import AuthChallenge, AuthResult
from pymirea.auth import MireaAuth as BaseMireaAuth

from .login_session import safe_path

# Top-level kcContext flags of the official MAX page; the only values ever copied into diagnostics.
_MAX_FLAGS = ("showSkip", "isEnrollment", "phoneNotRegistered", "noPhoneNumber", "deliveryPending", "sendFailed")
_MAX_SKIP_PATH = "/realms/mirea/login-actions/required-action"
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_JSON_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\.)*"\Z', re.S)

# OIDC form_post (response_mode=form_post) callback back to Pulse. Values are never logged.
_CALLBACK_NETLOCS = {"pulse.mirea.ru", "pulse.mirea.ru:443"}
_CALLBACK_PATH = "/api/mireaauth"
_ISSUER = "https://sso.mirea.ru/realms/mirea"
_CALLBACK_FIELDS = {"code", "state", "session_state", "iss", "id_token"}
# Field *names* only that may appear in diagnostics (intersection), never values.
_KNOWN_FIELD_NAMES = _CALLBACK_FIELDS | {"error", "error_description", "skip", "login"}
_MAX_FORM_HTML = 256 * 1024
_MAX_FORMS_REPORTED = 4
_MAX_CALLBACK_CONTROLS = 8
_MAX_FIELD_VALUE = 8192


class MireaAuth(BaseMireaAuth):
    def _extract_keycloak_error(self, html: str) -> str | None:
        message = super()._extract_keycloak_error(html)
        if message is None:
            return None
        low = message.casefold()
        if any(word in low for word in ("парол", "password", "credential")):
            return "Неверный логин или пароль"
        if any(word in low for word in ("код", "code", "otp", "authenticator")):
            return "Неверный код подтверждения"
        return "SSO не подтвердила вход. Проверьте данные или повторите позже."

    def __init__(self):
        super().__init__()
        self.diagnostics = []
        self._max_skip_used = False
        self._callback_used = False
        self._callback_started = False
        self.checkpoint_handler = None
        self.candidate_cookies = None
        self.candidate_id = None
        self.checkpoint_error = False

    @staticmethod
    def _cookie_class(name):
        if name == ".AspNetCore.Cookies":
            return "aspnet_auth"
        if name.startswith(".AspNetCore.CookiesC"):
            return "aspnet_auth_chunk"
        if "Correlation" in name:
            return "correlation"
        if "Nonce" in name:
            return "nonce"
        if name.startswith(("KEYCLOAK_", "AUTH_SESSION_ID")):
            return "keycloak"
        return "moodle" if name == "MoodleSession" else "other"

    def _record_hop(self, response):
        headers = response.headers.get_list("set-cookie")
        cookies = []
        for raw in headers:
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except CookieError:
                continue
            for cookie in parsed.values():
                domain = (cookie["domain"] or response.url.host).lstrip(".")
                cookies.append({"class": self._cookie_class(cookie.key),
                                "domain": domain if domain in {"pulse.mirea.ru", "sso.mirea.ru", "online-edu.mirea.ru"} else "other",
                                "path": safe_path(cookie["path"] or "/"),
                                "secure": bool(cookie["secure"]), "httponly": bool(cookie["httponly"]),
                                "deleted": cookie["max-age"] == "0"})
        self.diagnostics.append({"event": "http_hop", "method": response.request.method,
                                 "host": response.url.host, "path": safe_path(response.url.path),
                                 "status": response.status_code, "set_cookie_count": len(headers), "cookies": cookies})

    def _capture_candidate(self):
        # Recovery refers to the first callback checkpoint, never a landing's
        # mutated/deleted cookies. Only an authenticated read may promote state.
        if self.candidate_cookies is not None:
            return
        cookies = {c.name: c.value for c in self.client.cookies.jar if c.value}
        if not cookies:
            return
        self.candidate_cookies = dict(cookies)
        if self.checkpoint_handler is not None:
            try:
                self.candidate_id = self.checkpoint_handler(dict(cookies))
            except Exception:
                self.checkpoint_error = True
                raise RuntimeError("Не удалось безопасно сохранить кандидата сессии.") from None

    def _terminal_failure(self, message):
        return AuthResult(False, message, cookies=self.candidate_cookies)

    # --- OIDC form_post callback: strict static recognition, no JS, no URL resolution ---------------

    @staticmethod
    def _is_callback_action(action) -> bool:
        """Exact Pulse callback observed in its correlation-cookie scope; never resolve against base."""
        if not isinstance(action, str) or any(ch in action for ch in "?#@\\") or any(ch.isspace() for ch in action):
            return False
        try:
            parsed = urlsplit(action)
            return (parsed.scheme == "https" and parsed.netloc.lower() in _CALLBACK_NETLOCS
                    and parsed.port in (None, 443) and parsed.path == _CALLBACK_PATH
                    and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    @staticmethod
    def _form_controls(form):
        return form.find_all(["input", "button", "select", "textarea"])

    @classmethod
    def _callback_fields(cls, soup, form) -> dict[str, str] | None:
        """Hidden callback values of a candidate form, or None on any deviation (fail closed)."""
        if str(form.get("method", "")).strip().lower() != "post":
            return None
        enctype = form.get("enctype")
        if enctype is not None and str(enctype).strip().lower() != "application/x-www-form-urlencoded":
            return None
        if not cls._is_callback_action(form.get("action")):
            return None
        controls = cls._form_controls(form)
        # Controls outside the form (e.g. form="..." association) make the submitted set ambiguous.
        if len(controls) != len(cls._form_controls(soup)) or len(controls) > _MAX_CALLBACK_CONTROLS:
            return None
        fields: dict[str, str] = {}
        for control in controls:
            kind = str(control.get("type", "submit" if control.name == "button" else "text")).strip().lower()
            if control.name in ("input", "button") and kind == "submit":
                # Keycloak's <noscript> Continue button: a submitter is never sent by form.submit().
                continue
            if control.name != "input" or kind != "hidden" or control.has_attr("disabled") or control.has_attr("form"):
                return None
            name = control.get("name")
            value = control.get("value", "")
            if not isinstance(name, str) or name not in _CALLBACK_FIELDS or name in fields:
                return None
            if not isinstance(value, str) or len(value) > _MAX_FIELD_VALUE:
                return None
            fields[name] = value
        if not fields.get("code") or not fields.get("state"):
            return None
        if "iss" in fields and fields["iss"] != _ISSUER:
            return None
        return fields

    def _inspect_forms(self, html: str, diagnostic: dict) -> dict[str, str] | None:
        """Record secret-free form shape; return callback fields only for exactly one valid callback form."""
        if len(html) > _MAX_FORM_HTML:
            diagnostic["form_parse_skipped"] = True
            return None
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")
        diagnostic["form_count"] = len(forms)
        shapes = []
        for form in forms[:_MAX_FORMS_REPORTED]:
            method = str(form.get("method", "")).strip().lower()
            names = [c.get("name") for c in self._form_controls(form) if isinstance(c.get("name"), str)]
            shapes.append({
                "method": method if method in ("post", "get") else "other",
                "action": "pulse_callback" if self._is_callback_action(form.get("action")) else "other",
                "known_fields": sorted(set(names) & _KNOWN_FIELD_NAMES),
                "unknown_field_count": sum(1 for n in names if n not in _KNOWN_FIELD_NAMES),
            })
        diagnostic["forms"] = shapes
        if len(forms) != 1:
            return None
        fields = self._callback_fields(soup, forms[0])
        diagnostic["callback_form"] = fields is not None
        return fields

    async def _submit_callback(self, fields: dict[str, str], referer: str) -> AuthResult:
        self._callback_used = True  # one callback POST per login attempt, set before sending
        self._callback_started = True
        target = "https://pulse.mirea.ru" + _CALLBACK_PATH
        response = await self.client.post(target, data=fields, headers={"Referer": referer}, follow_redirects=False)
        self._capture_candidate()  # persist before requesting any optional landing
        return await self._result(response)

    # --- Safe, non-executing reader for the serialized kcContext object literal -------------------

    @staticmethod
    def _skip_trivia(text: str, i: int) -> int | None:
        """Skip whitespace and JS comments; None on an unterminated comment."""
        n = len(text)
        while i < n:
            if text[i].isspace():
                i += 1
            elif text.startswith("//", i):
                end = text.find("\n", i)
                i = n if end < 0 else end + 1
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                if end < 0:
                    return None
                i = end + 2
            else:
                break
        return i

    @staticmethod
    def _skip_string(text: str, i: int) -> int | None:
        """text[i] is a quote; return index after the closing quote, None if unterminated."""
        quote, i, n = text[i], i + 1, len(text)
        while i < n:
            if text[i] == "\\":
                i += 2
            elif text[i] == quote:
                return i + 1
            elif quote != "`" and text[i] == "\n":
                return None
            else:
                i += 1
        return None

    @classmethod
    def _skip_value(cls, text: str, i: int) -> int | None:
        """Return index of the ',' or '}' ending a member value at the current depth (None if malformed)."""
        pairs, stack, n = {"}": "{", "]": "[", ")": "("}, [], len(text)
        while i < n:
            j = cls._skip_trivia(text, i)
            if j is None or j >= n:
                return None
            i, ch = j, text[j]
            if ch in "\"'`":
                i = cls._skip_string(text, i)
                if i is None:
                    return None
                continue
            if not stack and ch in ",}":
                return i
            if ch in "{[(":
                stack.append(ch)
            elif ch in "}])":
                if not stack or stack.pop() != pairs[ch]:
                    return None
            i += 1
        return None

    @classmethod
    def _object_members(cls, text: str, start: int) -> dict[str, str] | None:
        """Direct members of the object literal at text[start] == '{' as raw value source.

        Nested objects are never flattened, so markers inside unrelated members are not
        authority. Duplicate keys, spreads, getters or any unparsable syntax fail closed (None).
        """
        members, i, n = {}, start + 1, len(text)
        while True:
            i = cls._skip_trivia(text, i)
            if i is None or i >= n:
                return None
            if text[i] == "}":
                return members
            if text[i] in "\"'":
                end = cls._skip_string(text, i)
                if end is None or text[i] != '"':
                    return None
                try:
                    key = json.loads(text[i:end])
                except ValueError:
                    return None
                i = end
            else:
                match = _IDENT.match(text, i)
                if not match:
                    return None
                key, i = match.group(0), match.end()
            i = cls._skip_trivia(text, i)
            if i is None or i >= n:
                return None
            if text[i] == ":":
                value_start = i + 1
            elif text[i] == "(":
                value_start = i  # method shorthand: irrelevant value, still must balance
            else:
                return None
            end = cls._skip_value(text, value_start)
            if end is None or key in members:
                return None
            members[key] = text[value_start:end].strip()
            i = end + 1 if text[end] == "," else end

    @classmethod
    def _kc_context_members(cls, html: str) -> dict[str, str] | None:
        starts = [m.end() - 1 for m in re.finditer(r"(?<![\w$.])(?:window\.)?kcContext\s*=\s*\{", html)]
        if len(starts) != 1:
            return None  # absent or ambiguous
        return cls._object_members(html, starts[0])

    @staticmethod
    def _literal_bool(raw: str | None):
        """True/False for an exact boolean literal, None if absent, ... (Ellipsis) if anything else."""
        if raw is None:
            return None
        return {"true": True, "false": False}.get(raw, ...)

    def _optional_max_skip_action(self, html: str, diagnostic: dict) -> str | None:
        """Return the trusted skip action only when the server visibly offers Skip for optional MAX setup."""
        members = self._kc_context_members(html)
        diagnostic["max_context_parsed"] = members is not None
        if members is None:
            return None
        page_raw = members.get("pageId")
        if not page_raw or not _JSON_STRING.match(page_raw):
            return None
        if json.loads(page_raw).removesuffix(".ftl") != "login-max-otp":
            return None
        flags = {name: self._literal_bool(members.get(name)) for name in _MAX_FLAGS}
        diagnostic["max_flags"] = {name: value for name, value in flags.items() if isinstance(value, bool)}
        if any(value is ... for value in flags.values()):
            return None  # non-boolean flag: fail closed
        offered = (flags["showSkip"] is True and not flags["deliveryPending"] and not flags["sendFailed"]
                   and any(flags[name] for name in ("isEnrollment", "phoneNotRegistered", "noPhoneNumber")))
        diagnostic["max_skip_offered"] = offered
        if not offered:
            return None
        url_raw = members.get("url")
        url_members = self._object_members(url_raw, 0) if url_raw and url_raw.startswith("{") else None
        action_raw = (url_members or {}).get("loginAction")
        if not action_raw or not _JSON_STRING.match(action_raw):
            return None
        action = json.loads(action_raw)
        if not self._allowed(action, credential_post=True) or urlsplit(action).path != _MAX_SKIP_PATH:
            diagnostic["max_skip_action_trusted"] = False
            return None
        return action

    async def _submit_max_skip(self, action: str, referer: str) -> AuthResult:
        self._max_skip_used = True  # one skip POST per login attempt, set before sending
        response = await self.client.post(action, data={"skip": "true"}, headers={"Referer": referer}, follow_redirects=False)
        return await self._result(response)

    @staticmethod
    def _page_id(html: str) -> str | None:
        match = re.search(r'"pageId"\s*:\s*"([a-zA-Z0-9_.-]+)"', html)
        return match.group(1).removesuffix(".ftl") if match else None

    def _extract_otp_challenge(self, html: str, *, base_url: str | None = None) -> AuthChallenge | None:
        page_id = self._page_id(html)
        action = self._extract_login_action(html)
        if page_id == "email-code-form" and action:
            return AuthChallenge(kind="email_code", action_url=action, field_name="emailCode", hidden_fields={"login": "true"})
        if page_id == "login-max-otp" and action:
            if any(re.search(r'"' + key + r'"\s*:\s*true', html) for key in ("isEnrollment", "phoneNotRegistered", "noPhoneNumber", "deliveryPending", "sendFailed")):
                return None  # this MAX page is setup/delivery, not a request for another email code
            return AuthChallenge(kind="max_otp", action_url=action, field_name="code", hidden_fields={"login": "true"})
        if page_id == "login-otp" and action:
            return AuthChallenge(kind="otp", action_url=action, field_name="otp", hidden_fields={})
        if page_id is not None:
            return None  # marker strings elsewhere do not establish which form is active
        return super()._extract_otp_challenge(html, base_url=base_url)

    @staticmethod
    def _allowed(url: str, *, credential_post: bool = False) -> bool:
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
                return False
            if credential_post:
                return parsed.hostname == "sso.mirea.ru" and parsed.path.startswith("/realms/mirea/login-actions/")
            return parsed.hostname in {"sso.mirea.ru", "pulse.mirea.ru"}
        except ValueError:
            return False

    async def _follow(self, response: httpx.Response) -> httpx.Response:
        for _ in range(12):
            self._record_hop(response)
            if response.url.host == "pulse.mirea.ru" and response.url.path in {_CALLBACK_PATH, "/signin-oidc"}:
                self._callback_started = True
            if self._callback_started:
                self._capture_candidate()
            if not response.is_redirect:
                return response
            target = urljoin(str(response.url), response.headers.get("location", ""))
            if not self._allowed(target):
                raise ValueError("untrusted_auth_redirect")
            # Never re-send a credential POST to a redirected endpoint.
            response = await self.client.get(target, follow_redirects=False)
        raise ValueError("too_many_auth_redirects")

    async def prepare_login(self) -> bool:
        self._login_action = None
        self._login_referer = None
        response = await self.client.get(
            "https://pulse.mirea.ru/api/auth/login", params={"redirectUri": "/"}, follow_redirects=False
        )
        response = await self._follow(response)
        action = self._extract_login_action(response.text)
        if response.status_code == 200 and action and self._allowed(action, credential_post=True):
            self._login_action = action
            self._login_referer = str(response.url)
            return True
        return False

    async def _result(self, response: httpx.Response) -> AuthResult:
        submitted = sorted(parse_qs(response.request.content.decode("utf-8", errors="replace"))) if response.request.method == "POST" else []
        request_url = response.request.url
        is_callback = (response.request.method == "POST" and request_url.host == "pulse.mirea.ru"
                       and request_url.path == _CALLBACK_PATH)
        response = await self._follow(response)
        phase = "callback" if is_callback else ("password" if "password" in submitted else ("skip" if submitted == ["skip"] else ("otp" if submitted else "response")))
        diagnostic = {"phase": phase,
                      "status": response.status_code, "host": response.url.host, "path": response.url.path,
                      "page_id": self._page_id(response.text), "submitted_fields": submitted,
                      "server_error_present": bool(re.search(r'"type"\s*:\s*"error"', response.text)),
                      "callback": is_callback,
                      "candidate_available": bool(self.candidate_cookies),
                      "checkpoint_saved": self.candidate_id is not None and not self.checkpoint_error}
        self.diagnostics.append(diagnostic)
        if response.status_code != 200:
            return self._terminal_failure(f"Конечная страница вернула HTTP {response.status_code}; сохранённый кандидат можно проверить без нового входа.")
        if response.url.host == "sso.mirea.ru":
            if self._callback_started:
                diagnostic["callback_repeated"] = True
                return self._terminal_failure("SSO снова запросила вход после callback; кандидат сохранён для проверки, код повторно не запрашивается.")
            if diagnostic["page_id"] == "login-max-otp" and self._max_skip_used:
                diagnostic["max_skip_repeated"] = True
                return AuthResult(False, "SSO снова запросила MAX после пропуска настройки; вход остановлен. Завершите его на официальном сайте.")
            if diagnostic["page_id"] is None:
                callback_fields = self._inspect_forms(response.text, diagnostic)
                if callback_fields is not None:
                    if self._callback_used:
                        diagnostic["callback_repeated"] = True
                        return AuthResult(False, "SSO повторно вернула форму возврата в Pulse; вход остановлен. Завершите его на официальном сайте.")
                    # Standard OIDC form_post: hand the server-owned code back to Pulse, never to /token.
                    return await self._submit_callback(callback_fields, str(response.url))
            challenge = self._extract_otp_challenge(response.text, base_url=str(response.url))
            if challenge:
                if not self._allowed(challenge.action_url, credential_post=True):
                    return AuthResult(False, "SSO вернула недопустимый адрес подтверждения.")
                challenge.referer = str(response.url)
                if challenge.kind == "email_code":
                    # The real Keycloakify form adds this submit-action field via JS.
                    challenge.hidden_fields = {**challenge.hidden_fields, "login": "true"}
                diagnostic.update(challenge_kind=challenge.kind, code_field=challenge.field_name, submit_login=challenge.hidden_fields.get("login") == "true")
                return AuthResult(False, self._extract_keycloak_error(response.text) or "Требуется код подтверждения", challenge=challenge)
            if diagnostic["page_id"] == "login-max-otp":
                skip_action = self._optional_max_skip_action(response.text, diagnostic)
                diagnostic["max_skip_used"] = bool(skip_action)
                if skip_action:
                    # User-requested: honor only the server-rendered optional Skip, exactly once.
                    return await self._submit_max_skip(skip_action, str(response.url))
                return AuthResult(False, "SSO перешла к настройке или доставке подтверждения MAX. Это не повтор почтового кода; вход ещё не завершён.")
            return AuthResult(False, self._extract_keycloak_error(response.text) or "МИРЭА требует дополнительного действия. Завершите его на официальном сайте.")
        cookies = {cookie.name: cookie.value for cookie in self.client.cookies.jar if cookie.value}
        if response.url.host == "pulse.mirea.ru" and cookies.get(".AspNetCore.Cookies"):
            return AuthResult(True, "Авторизация успешна", cookies=cookies)
        return self._terminal_failure("Доступ Pulse ещё не подтверждён; сохранённый кандидат можно проверить отдельно.")

    async def login(self, username: str, password: str) -> AuthResult:
        self._max_skip_used = False  # guards reset only on an explicit new login
        self._callback_used = False
        self._callback_started = False
        self.candidate_cookies = None
        self.candidate_id = None
        self.checkpoint_error = False
        try:
            if not await self.prepare_login():
                return AuthResult(False, "Штатный вход Pulse не вернул форму SSO; пароль не отправлялся.")
            response = await self.client.post(
                self._login_action,
                data={"username": username, "password": password},
                headers={"Referer": self._login_referer},
                follow_redirects=False,
            )
            return await self._result(response)
        except (httpx.HTTPError, ValueError, RuntimeError):
            return self._terminal_failure("Ошибка соединения, перенаправления или сохранения. Кандидат, если получен, не удалён.")

    async def submit_otp(self, challenge: AuthChallenge, otp_code: str, *, cookies: dict | None = None) -> AuthResult:
        if not self._allowed(challenge.action_url, credential_post=True):
            return AuthResult(False, "Недопустимый адрес подтверждения SSO.")
        if not otp_code.strip():
            return AuthResult(False, "Введите код подтверждения.", challenge=challenge)
        try:
            if cookies:
                self.client.cookies.update(cookies)
            payload = dict(challenge.hidden_fields or {})
            payload[challenge.field_name] = otp_code.strip()
            response = await self.client.post(
                challenge.action_url,
                data=payload,
                headers={"Referer": challenge.referer or challenge.action_url},
                follow_redirects=False,
            )
            return await self._result(response)
        except (httpx.HTTPError, ValueError, RuntimeError):
            if self._callback_started:
                if not self.checkpoint_error:
                    try:
                        self._capture_candidate()
                    except RuntimeError:
                        pass
                return self._terminal_failure("Ошибка после возврата в Pulse; проверьте сохранённого кандидата без нового кода.")
            return AuthResult(False, "Ошибка соединения до подтверждения; вход остановлен.")
