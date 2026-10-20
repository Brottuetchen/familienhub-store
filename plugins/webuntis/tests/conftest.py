"""Test setup: a throw-away data dir and database for the hub's modules, and a simulated WebUntis server.

Run with the hub's backend on the path:

    pip install pytest respx httpx fastapi sqlalchemy
    PYTHONPATH=/path/to/familienhub/backend pytest

The simulated server (``FakeUntis``) behaves like WebUntis: it checks the credentials, hands out session ids, rejects
calls without a valid session, and answers ``getTimetable`` & co. from the data a test gives it. It never decides what
the plugin does with the answers.
"""

import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="fh-webuntis-")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/hub.db")
os.environ.setdefault("SECRET_KEY", "pytest-secret")
os.environ.setdefault("VAPID_AUTO", "false")

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402

SERVER = "school.example.test"
SCHOOL = "demo-school"
USER, PASSWORD = "parent.demo", "pw-for-tests-123"
BASE = f"https://{SERVER}/WebUntis"


class FakeUntis:
    """A WebUntis server in a box. Attributes are the data it serves; ``calls`` records every JSON-RPC method."""

    def __init__(self):
        self.school, self.user, self.password = SCHOOL, USER, PASSWORD
        self.person_type, self.person_id = 12, 900          # 12 = parent account
        self.students = [{"id": 101, "displayName": "Mia Muster"}, {"id": 102, "displayName": "Ben Muster"}]
        self.timetable = {101: [], 102: []}                 # student id -> raw getTimetable rows
        self.timegrid = [{"day": 2, "timeUnits": [
            {"name": "1", "startTime": 800, "endTime": 845}, {"name": "2", "startTime": 850, "endTime": 935},
            {"name": "3", "startTime": 955, "endTime": 1040}, {"name": "4", "startTime": 1045, "endTime": 1130}]}]
        self.weekly = {}                                    # student id -> weekly/data payload
        self.deny_timetable = False                         # getTimetable answers -8509 (no right)
        self.rest_fails = False
        self.sessions, self.calls, self.logouts, self.logins = set(), [], 0, 0
        self.expire_next = False                            # the next authenticated call finds the session gone
        self.lookup_denied = True                           # getSubjects & co. are forbidden (students, parents)

    # -- helpers
    def _session_ok(self, request):
        cookie = request.headers.get("cookie", "")
        sid = next((p.split("=", 1)[1] for p in cookie.split("; ") if p.startswith("JSESSIONID=")), None)
        if self.expire_next:
            self.expire_next = False
            self.sessions.discard(sid)
        return sid in self.sessions

    def rpc(self, request):
        if request.url.params.get("school") != self.school:
            return httpx.Response(302, headers={"location": "https://x.example.test/"})
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if method == "authenticate":
            if params.get("user") != self.user or params.get("password") != self.password:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -8504, "message": "bad credentials"}})
            self.logins += 1
            sid = f"SID{self.logins}"
            self.sessions.add(sid)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "sessionId": sid, "personType": self.person_type, "personId": self.person_id, "klasseId": 7}})
        if not self._session_ok(request):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -8520, "message": "not authenticated"}})
        if method == "logout":
            self.logouts += 1
            self.sessions.clear()
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": None})
        if method == "getTimegridUnits":
            return self._ok(body, self.timegrid)
        if method == "getTimetable":
            if self.deny_timetable:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -8509, "message": "no right for timetable"}})
            options = params["options"]
            rows = [r for r in self.timetable.get(options["element"]["id"], [])
                    if options["startDate"] <= r["date"] <= options["endDate"]]
            return self._ok(body, rows)
        if method in ("getSubjects", "getRooms", "getTeachers"):
            if self.lookup_denied:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -8509, "message": "no right for getSubjects"}})
            return self._ok(body, [{"id": 1, "name": "M", "longName": "Maths"}, {"id": 2, "name": "204", "longName": "Room 204"}])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not found"}})

    @staticmethod
    def _ok(body, result):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def token(self, request):
        return httpx.Response(200, text="header.payload.signature") if self._session_ok(request) else httpx.Response(401)

    def app_data(self, request):
        if self.rest_fails or request.headers.get("authorization") != "Bearer header.payload.signature":
            return httpx.Response(500 if self.rest_fails else 401)
        return httpx.Response(200, json={"user": {"id": self.person_id, "name": USER, "students": self.students}})

    def weekly_data(self, request):
        sid = int(request.url.params["elementId"])
        return httpx.Response(200, json=self.weekly[sid]) if sid in self.weekly else httpx.Response(404)


@pytest.fixture
def untis():
    fake = FakeUntis()
    with respx.mock(using="httpx", assert_all_called=False) as router:
        router.post(f"{BASE}/jsonrpc.do").mock(side_effect=fake.rpc)
        router.get(f"{BASE}/api/token/new").mock(side_effect=fake.token)
        router.get(f"{BASE}/api/rest/view/v1/app/data").mock(side_effect=fake.app_data)
        router.get(f"{BASE}/api/public/timetable/weekly/data").mock(side_effect=fake.weekly_data)
        fake.router = router
        yield fake


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("WEBUNTIS_SERVER", SERVER)
    monkeypatch.setenv("WEBUNTIS_SCHOOL", SCHOOL)
    monkeypatch.setenv("WEBUNTIS_USERNAME", USER)
    monkeypatch.setenv("WEBUNTIS_PASSWORD", PASSWORD)
    monkeypatch.delenv("WEBUNTIS_NOTIFY", raising=False)
    monkeypatch.delenv("WEBUNTIS_NOTIFY_DAYS", raising=False)


def lesson(day, start, end, subject="Maths", code=None, room="204", teacher="Smith", **extra):
    """A raw ``getTimetable`` row as WebUntis sends it (ids plus the requested name fields)."""
    row = {"id": hash((day, start, subject)) % 100000, "date": day, "startTime": start, "endTime": end,
           "kl": [{"id": 7, "name": "5a", "longname": "Class 5a"}],
           "su": [{"id": 1, "name": subject[:3], "longname": subject}],
           "ro": [{"id": 2, "name": room, "longname": room}] if room else [],
           "te": [{"id": 3, "name": teacher, "longname": teacher}] if teacher else [], "lstype": "ls"}
    if code:
        row["code"] = code
    row.update(extra)
    return row
