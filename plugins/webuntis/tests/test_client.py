import asyncio
from datetime import date

import httpx
import pytest

from fh_plugin_webuntis.client import WebUntisClient, WebUntisError

from .conftest import BASE, PASSWORD, SCHOOL, SERVER, USER, lesson

run = asyncio.run
FIRST, LAST = date(2026, 10, 19), date(2026, 10, 25)


def client(**over):
    args = dict(server=SERVER, school=SCHOOL, username=USER, password=PASSWORD)
    args.update(over)
    return WebUntisClient(args["server"], args["school"], args["username"], args["password"])


async def lessons_of(c, student="101"):
    async with c:
        return await c.lessons(student, FIRST, LAST)


def test_login_cookie_and_logout_always(untis):
    untis.timetable[101] = [lesson(20261020, 800, 845)]
    out = run(lessons_of(client()))
    assert out[0]["title"] == "Maths"
    assert untis.calls == ["authenticate", "getTimetable", "logout"]
    request = untis.router.calls[1].request
    assert "JSESSIONID=SID1" in request.headers["cookie"] and "schoolname=_" in request.headers["cookie"]
    assert request.url.params["school"] == SCHOOL


def test_logout_even_when_the_call_fails(untis):
    untis.deny_timetable = True  # and no weekly data either -> fails
    with pytest.raises(WebUntisError):
        run(lessons_of(client()))
    assert untis.logouts == 1


def test_session_expiry_logs_in_again_once(untis):
    untis.timetable[101] = [lesson(20261020, 800, 845)]

    async def go():
        async with client() as c:
            await c.login()
            untis.expire_next = True
            return await c.lessons("101", FIRST, LAST)

    assert run(go())[0]["title"] == "Maths"
    assert untis.logins == 2


def test_request_carries_the_documented_options(untis):
    run(lessons_of(client()))
    import json
    body = json.loads(next(c.request for c in untis.router.calls if b"getTimetable" in c.request.content).content)
    options = body["params"]["options"]
    assert options["element"] == {"id": 101, "type": 5} and options["startDate"] == 20261019 and options["endDate"] == 20261025
    assert options["showInfo"] and options["showSubstText"] and options["showLsText"]
    assert "longname" in options["subjectFields"]


@pytest.mark.parametrize("kwargs, kind", [
    ({"password": "wrong"}, "credentials"),
    ({"username": "nobody"}, "credentials"),
    ({"school": "other-school"}, "not_found"),
])
def test_login_errors_are_classified(untis, kwargs, kind):
    with pytest.raises(WebUntisError) as err:
        run(lessons_of(client(**kwargs)))
    assert err.value.kind == kind


@pytest.mark.parametrize("answer, kind", [
    (httpx.Response(500), "server"),
    (httpx.Response(503, text="maintenance"), "server"),
    (httpx.Response(404), "not_found"),
    (httpx.Response(401), "credentials"),
    (httpx.Response(200, text="<html>Login</html>"), "malformed"),
    (httpx.Response(200, json=["not", "an", "object"]), "malformed"),
    (httpx.Response(200, json={"jsonrpc": "2.0"}), "malformed"),
    (httpx.Response(200, json={"error": {"code": -8504, "message": "x"}}), "credentials"),
    (httpx.Response(200, json={"error": {"code": -7777, "message": "school not found"}}), "not_found"),
    (httpx.Response(200, json={"error": {"code": -1, "message": "boom"}}), "rpc"),
    (httpx.Response(200, json={"result": {"sessionId": ""}}), "malformed"),
    (httpx.ConnectError("down"), "server"),
    (httpx.ReadTimeout("slow"), "server"),
])
def test_failures_never_escape_as_other_exceptions(answer, kind):
    import respx
    with respx.mock(using="httpx", assert_all_called=False) as router:
        router.post(f"{BASE}/jsonrpc.do").mock(side_effect=[answer] * 5)
        with pytest.raises(WebUntisError) as err:
            run(lessons_of(client()))
    assert err.value.kind == kind


def test_error_text_has_no_secrets(untis):
    with pytest.raises(WebUntisError) as err:
        run(lessons_of(client(password="top-secret-pw")))
    text = repr(err.value) + str(err.value)
    assert "top-secret-pw" not in text and SERVER not in text and USER not in text


def test_student_account_sees_itself(untis):
    untis.person_type, untis.person_id = 5, 101

    async def go():
        async with client() as c:
            return await c.students()

    assert run(go()) == [{"id": "101", "name": USER}]
    assert "api/token/new" not in str([c.request.url for c in untis.router.calls])


def test_parent_account_lists_its_students(untis):
    async def go():
        async with client() as c:
            return await c.students()

    assert run(go()) == [{"id": "101", "name": "Mia Muster"}, {"id": "102", "name": "Ben Muster"}]


@pytest.mark.parametrize("payload", [{"user": {}}, {"user": {"students": "x"}}, {"user": {"students": [{"name": "no id"}]}}, [1]])
def test_odd_app_data_gives_no_students(untis, payload):
    untis.router.get(f"{BASE}/api/rest/view/v1/app/data").mock(return_value=httpx.Response(200, json=payload))

    async def go():
        async with client() as c:
            return await c.students()

    assert run(go()) == []


def test_students_when_the_rest_api_is_down(untis):
    untis.rest_fails = True

    async def go():
        async with client() as c:
            return await c.students()

    assert run(go()) == []


def test_no_right_for_getTimetable_uses_the_weekly_data(untis):
    untis.deny_timetable = True
    untis.weekly[101] = {"data": {"result": {"data": {
        "elements": [{"type": 3, "id": 1, "name": "M", "longName": "Maths"}],
        "elementPeriods": {"101": [{"date": 20261020, "startTime": 800, "endTime": 845, "cellState": "CANCEL",
                                    "elements": [{"type": 3, "id": 1}]}]}}}}}
    out = run(lessons_of(client()))
    assert out[0]["status"] == "cancelled" and out[0]["title"] == "Maths"
    assert untis.logouts == 1


def test_names_are_looked_up_only_when_needed(untis):
    untis.lookup_denied = False
    row = lesson(20261020, 800, 845)
    row["su"], row["ro"] = [{"id": 1}], [{"id": 2}]
    untis.timetable[101] = [row]
    out = run(lessons_of(client()))
    assert out[0]["title"] == "Maths" and "getSubjects" in untis.calls
    untis.calls.clear()
    untis.timetable[101] = [lesson(20261020, 800, 845)]
    run(lessons_of(client()))
    assert "getSubjects" not in untis.calls


def test_forbidden_lookup_is_not_an_error(untis):
    row = lesson(20261020, 800, 845)
    row["su"], row["te"] = [{"id": 1}], []
    untis.timetable[101] = [row]
    assert run(lessons_of(client()))[0]["title"] == "?"
