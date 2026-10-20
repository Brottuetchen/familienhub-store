"""A small WebUntis client (JSON-RPC at ``/WebUntis/jsonrpc.do``). httpx only, no hub imports.

Every failure becomes a ``WebUntisError`` with a ``kind``; its message never contains the address, the school, the
user name or the password. One client = one session: ``async with WebUntisClient(...) as c`` logs in on ``login()``
and always logs out when the block ends.
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx

from .parsing import needs_names, parse_lessons, parse_timegrid, parse_weekly, untis_day

TIMEOUT = 8.0  # seconds per request
CLIENT_NAME = "Familienhub"
USER_AGENT = "Familienhub-WebUntis/1.0"
STUDENT = 5  # element type of a student
CODE_BAD_CREDENTIALS, CODE_NOT_AUTHENTICATED, CODE_NO_RIGHT = -8504, -8520, -8509

KINDS = ("server", "not_found", "credentials", "no_right", "session", "malformed", "rpc")


class WebUntisError(Exception):
    """``kind``: server (unreachable, timeout, 5xx), not_found (wrong server or school), credentials, no_right,
    session (expired), malformed (not WebUntis JSON) or rpc (any other error code of WebUntis)."""

    def __init__(self, kind: str, code: Optional[int] = None):
        super().__init__(kind)
        self.kind, self.code = kind, code


def _error_of(payload: Dict[str, Any]) -> Optional[WebUntisError]:
    err = payload.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    message = str(err.get("message", "")).lower()
    if code == CODE_BAD_CREDENTIALS or "bad credentials" in message:
        return WebUntisError("credentials", code)
    if code == CODE_NOT_AUTHENTICATED or "not authenticated" in message:
        return WebUntisError("session", code)
    if code == CODE_NO_RIGHT or "no right" in message:
        return WebUntisError("no_right", code)
    if "school" in message and ("not found" in message or "unknown" in message or "invalid" in message):
        return WebUntisError("not_found", code)
    return WebUntisError("rpc", code if isinstance(code, int) else None)


class WebUntisClient:
    def __init__(self, server: str, school: str, username: str, password: str, *, timeout: float = TIMEOUT):
        self.server, self.school, self._user, self._password = server, school, username, password
        self.person_id: Optional[int] = None
        self.person_type: Optional[int] = None
        self._session: Optional[str] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._timeout = timeout
        self._logged_in = False

    async def __aenter__(self) -> "WebUntisClient":
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), follow_redirects=False,
                                       headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self.logout()
        finally:
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    # --- plumbing ---------------------------------------------------------------------------------------------

    def _cookies(self) -> Dict[str, str]:
        if not self._session:
            return {}
        school64 = "_" + base64.b64encode(self.school.encode("latin-1", "ignore")).decode()
        return {"Cookie": f"JSESSIONID={self._session}; schoolname={school64}"}

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._http is not None, "use the client as a context manager"
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise WebUntisError("server") from exc
        except httpx.HTTPError as exc:
            raise WebUntisError("server") from exc
        if response.status_code in (301, 302, 303, 307, 308, 404):
            raise WebUntisError("not_found")  # wrong server or school: WebUntis redirects to its start page
        if response.status_code >= 500:
            raise WebUntisError("server")
        if response.status_code in (401, 403):
            raise WebUntisError("credentials")
        return response

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Any:
        response = await self._send(
            "POST", f"https://{self.server}/WebUntis/jsonrpc.do", params={"school": self.school},
            headers=self._cookies(),
            content=json.dumps({"id": CLIENT_NAME, "method": method, "params": params, "jsonrpc": "2.0"}))
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebUntisError("malformed") from exc
        if not isinstance(payload, dict):
            raise WebUntisError("malformed")
        problem = _error_of(payload)
        if problem is not None:
            raise problem
        if "result" not in payload:
            raise WebUntisError("malformed")
        return payload["result"]

    async def login(self) -> None:
        """``authenticate``: session id (kept as a cookie), ``personId`` and ``personType``."""
        result = await self._rpc("authenticate", {"user": self._user, "password": self._password, "client": CLIENT_NAME})
        session = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session, str) or not session:
            raise WebUntisError("malformed")
        self._session, self._logged_in = session, True
        pid, ptype = result.get("personId"), result.get("personType")
        self.person_id = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        self.person_type = ptype if isinstance(ptype, int) and not isinstance(ptype, bool) else None

    async def logout(self) -> None:
        """Best effort, never raises: WebUntis asks clients to free their session as soon as possible."""
        if not self._logged_in or self._http is None:
            return
        self._logged_in = False
        try:
            await self._rpc("logout", {})
        except Exception:  # noqa: BLE001 - nothing to do if the server is already gone
            pass
        self._session = None

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """A JSON-RPC call; when the session has expired (-8520) it logs in again once and repeats the call."""
        if not self._logged_in:
            await self.login()
        try:
            return await self._rpc(method, params or {})
        except WebUntisError as exc:
            if exc.kind != "session":
                raise
        await self.login()
        return await self._rpc(method, params or {})

    # --- data -------------------------------------------------------------------------------------------------

    async def students(self) -> List[Dict[str, str]]:
        """The students this account can see ``[{id, name}]``.

        A student account sees itself (``personType`` 5). For a parent account the students are read from the web
        client's app data (``/WebUntis/api/rest/view/v1/app/data``, ``user.students``), reached with a token from
        ``/WebUntis/api/token/new``. Anything else yields an empty list."""
        if not self._logged_in:
            await self.login()
        if self.person_type == STUDENT and self.person_id is not None:
            return [{"id": str(self.person_id), "name": self._user}]
        try:
            token = (await self._send("GET", f"https://{self.server}/WebUntis/api/token/new", headers=self._cookies())).text.strip()
            if not token or token.startswith("{") or token.startswith("<"):
                return []
            response = await self._send("GET", f"https://{self.server}/WebUntis/api/rest/view/v1/app/data",
                                        headers={**self._cookies(), "Authorization": f"Bearer {token}"})
            return _students_of(response.json())
        except (WebUntisError, ValueError):
            return []

    async def timegrid(self) -> Dict[str, int]:
        if not self._logged_in:
            await self.login()  # a rejected login is not "no time grid"
        try:
            return parse_timegrid(await self.call("getTimegridUnits"))
        except WebUntisError:
            return {}

    async def _names(self, raw: List[Any]) -> Dict[str, Dict[Any, str]]:
        """Only when a lesson carries bare ids: look names up (may be forbidden for students and parents)."""
        methods = {"su": "getSubjects", "ro": "getRooms", "te": "getTeachers"}
        found: Dict[str, Dict[Any, str]] = {}
        for kind, wanted in needs_names(raw).items():
            try:
                rows = await self.call(methods[kind])
            except WebUntisError:
                continue
            table = {r.get("id"): (r.get("longName") or r.get("longname") or r.get("name") or "")
                     for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
            found[kind] = {i: table[i] for i in wanted if table.get(i)}
        return found

    async def lessons(self, student_id: str, first: date, last: date,
                      periods: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
        """Lessons of one student between two dates (inclusive), hub shape. ``getTimetable`` first; an account that may
        not use it (-8509) gets the web client's weekly data instead."""
        try:
            element_id = int(student_id)
        except ValueError as exc:
            raise WebUntisError("rpc") from exc
        options = {"element": {"id": element_id, "type": STUDENT}, "startDate": untis_day(first), "endDate": untis_day(last),
                   "showInfo": True, "showSubstText": True, "showLsText": True, "showLsNumber": True,
                   "showStudentgroup": True, "klasseFields": ["id", "name", "longname"],
                   "roomFields": ["id", "name", "longname"], "subjectFields": ["id", "name", "longname"],
                   "teacherFields": ["id", "name", "longname"]}
        try:
            raw = await self.call("getTimetable", {"options": options})
        except WebUntisError as exc:
            if exc.kind != "no_right":
                raise
            return await self._weekly(element_id, first, last, periods)
        if not isinstance(raw, list):
            raise WebUntisError("malformed")
        return parse_lessons(raw, first, last, periods=periods, names=await self._names(raw))

    async def _weekly(self, element_id: int, first: date, last: date, periods: Optional[Dict[str, int]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        monday = first - timedelta(days=first.weekday())
        while monday <= last:
            response = await self._send(
                "GET", f"https://{self.server}/WebUntis/api/public/timetable/weekly/data",
                params={"elementType": STUDENT, "elementId": element_id, "date": monday.isoformat(), "formatId": 1},
                headers=self._cookies())
            try:
                out += parse_weekly(response.json(), element_id, first, last, periods=periods)
            except ValueError as exc:  # also JSON errors
                raise WebUntisError("no_right" if "error" in response.text[:200].lower() else "malformed") from exc
            monday += timedelta(days=7)
        return out


def _students_of(payload: Any) -> List[Dict[str, str]]:
    user = payload.get("user") if isinstance(payload, dict) else None
    rows = (user or {}).get("students") if isinstance(user, dict) else None
    if rows is None and isinstance(payload, dict):
        rows = payload.get("students")
    out: List[Dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("id") in (None, ""):
            continue
        name = next((str(row[k]).strip() for k in ("displayName", "name", "longName", "fullName") if row.get(k)), "")
        if not name:
            name = " ".join(str(row[k]) for k in ("foreName", "firstName", "longName", "lastName") if row.get(k)).strip()
        out.append({"id": str(row["id"]), "name": name or str(row["id"])})
    return out
