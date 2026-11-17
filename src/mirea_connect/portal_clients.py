"""Thin adapters for observed Pulse cookie auth and Moodle OIDC form_post.

No password/OTP prompts, generic required-action skips or token redemption here.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pymirea.grades import MireaGrades as BaseGrades
from pymirea.sdo import MireaSDO as BaseSDO


class MireaGrades(BaseGrades):
    async def _ensure_aspnet_cookie(self):
        """Check actual authenticated data, not a legacy cookie name."""
        if getattr(self, "_portal_access_checked", False):
            return True, None
        logs, error = await self._get_visiting_logs()
        if error or not logs:
            return False, "Pulse не подтвердил доступ к журналам; сохранённый кандидат не удалён."
        self._portal_access_checked = True
        return True, None


class MireaSDO(BaseSDO):
    _TARGET = "https://online-edu.mirea.ru/auth/oidc/"
    _FIELDS = {"code", "state", "session_state", "iss", "id_token"}

    @staticmethod
    def _safe_url(url):
        try:
            parsed = urlsplit(url)
            return (parsed.scheme == "https" and not parsed.username and not parsed.password
                    and parsed.port in (None, 443) and not parsed.fragment
                    and ((parsed.hostname == "online-edu.mirea.ru" and
                          (parsed.path in {"/", "/my/", "/auth/oidc/", "/login/index.php"}))
                         or (parsed.hostname == "sso.mirea.ru" and parsed.path.startswith("/realms/mirea/"))))
        except ValueError:
            return False

    @classmethod
    def _form_fields(cls, response):
        if response.url.host != "sso.mirea.ru" or len(response.content) > 256 * 1024:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        forms = soup.find_all("form")
        if len(forms) != 1:
            return None
        form = forms[0]
        if (str(form.get("method", "")).lower() != "post" or form.get("action") != cls._TARGET
                or str(form.get("enctype", "application/x-www-form-urlencoded")).lower() != "application/x-www-form-urlencoded"):
            return None
        controls = form.find_all(["input", "button", "select", "textarea"])
        if len(controls) > 8 or len(controls) != len(soup.find_all(["input", "button", "select", "textarea"])):
            return None
        fields = {}
        for control in controls:
            kind = str(control.get("type", "submit" if control.name == "button" else "text")).lower()
            if control.name in {"input", "button"} and kind == "submit":
                continue
            if control.name != "input" or kind != "hidden" or control.has_attr("disabled") or control.has_attr("form"):
                return None
            name, value = control.get("name"), control.get("value", "")
            if name not in cls._FIELDS or name in fields or not isinstance(value, str) or len(value) > 8192:
                return None
            fields[name] = value
        if not fields.get("code") or not fields.get("state"):
            return None
        states = parse_qs(response.url.query.decode()).get("state", [])
        if states != [fields["state"]]:
            return None
        if "iss" in fields and fields["iss"] != "https://sso.mirea.ru/realms/mirea":
            return None
        return fields

    async def _checked_redirects(self, response):
        for _ in range(12):
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                raise ValueError("Missing OIDC redirect")
            target = urljoin(str(response.url), location)
            if not self._safe_url(target):
                raise ValueError("Untrusted OIDC redirect")
            if response.status_code in {307, 308} and response.request.method == "POST":
                raise ValueError("Credential-bearing POST redirects are not replayed")
            response = await self.client.get(target, follow_redirects=False)
        raise ValueError("OIDC redirect limit")

    async def bootstrap_session(self):
        """Follow the exact Moodle-owned callback once with the existing SSO jar."""
        try:
            response = await self.client.get(self._TARGET, follow_redirects=False)
            response = await self._checked_redirects(response)
            fields = self._form_fields(response) if response.status_code == 200 else None
            if fields is not None:
                response = await self.client.post(self._TARGET, data=fields, follow_redirects=False)
                response = await self._checked_redirects(response)
            if response.url.host != "online-edu.mirea.ru" or response.status_code != 200 or "/login" in response.url.path:
                return False
            self._store_moodle_session()
            return self._has_moodle_cookie()
        except (httpx.HTTPError, ValueError):
            return False
