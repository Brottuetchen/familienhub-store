import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.plugins.manifest import validate_manifest

from fh_plugin_webuntis import plugin as module
from fh_plugin_webuntis.plugin import WebUntisPlugin

from .conftest import BASE, PASSWORD, SCHOOL, SERVER, USER, lesson

run = asyncio.run
ROOT = Path(__file__).resolve().parents[1]
MIA = SimpleNamespace(id=5, name="Mia", person_id="101")
FIRST, LAST = date(2026, 10, 19), date(2026, 10, 25)


def week(untis):
    untis.timetable[101] = [
        lesson(20261020, 800, 845),
        lesson(20261020, 850, 935, subject="English", code="cancelled", substText="Teacher ill"),
        lesson(20261020, 955, 1040, subject="Art", code="irregular", substText="Cover", room="R12"),
        lesson(20261031, 800, 845),  # outside the asked week
    ]


# --- manifest and code agree ---------------------------------------------------------------------------------


def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = WebUntisPlugin()
    assert plugin.id == manifest["id"] == "webuntis" and manifest["version"] == "1.0.0"
    assert manifest["min_hub_version"] == "1.0.0-beta.3"
    assert manifest["entry_point"] == "fh_plugin_webuntis.plugin:WebUntisPlugin"
    assert {s.key for s in plugin.settings} <= {s["key"] for s in manifest["settings"]}
    assert set(plugin.capabilities) <= set(manifest["capabilities"]) == {"timetable"}
    required = {s["key"] for s in manifest["settings"] if s["required"]}
    assert required == {"WEBUNTIS_SERVER", "WEBUNTIS_SCHOOL", "WEBUNTIS_USERNAME", "WEBUNTIS_PASSWORD"}
    assert {s["key"] for s in manifest["settings"] if s["secret"]} == {"WEBUNTIS_PASSWORD"}
    assert plugin.schedule_minutes == 15 and plugin.routers() == []


def test_the_hub_sees_the_hooks_and_the_job():
    from app.plugins import jobs
    from app.plugins.sources import overrides
    plugin = WebUntisPlugin()
    assert overrides(plugin, "timetable") and overrides(plugin, "timetable_people") and plugin.has_test
    assert jobs.has_job(plugin) and jobs.interval_seconds(plugin) == 15 * 60


def test_the_pyproject_entry_point_matches_the_manifest():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'webuntis = "fh_plugin_webuntis.plugin:WebUntisPlugin"' in text and "dependencies = []" in text


def test_loads_with_the_hubs_installer(tmp_path):
    """The installer's own loader, in a clean interpreter (it refuses package names that are already importable)."""
    import subprocess
    import sys
    import shutil

    folder = tmp_path / "src"
    shutil.copytree(ROOT / "fh_plugin_webuntis", folder / "fh_plugin_webuntis", ignore=shutil.ignore_patterns("__pycache__"))
    code = f"""
import json, os, sys
sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in repr(f).lower()]  # a developer's `pip install -e`
from pathlib import Path
from fastapi import FastAPI
from app.plugins.manifest import validate_manifest
from app.plugins.installer import load_plugin
manifest = validate_manifest(json.loads(Path({str(ROOT / 'familienhub-plugin.json')!r}).read_text()))
plugin = load_plugin(FastAPI(), manifest, Path({str(folder)!r}))
print("LOADED", plugin.id, sorted(plugin.capabilities), plugin.schedule_minutes, plugin.has_test)
"""
    env = {**__import__("os").environ, "DATA_DIR": str(tmp_path / "data"), "SECRET_KEY": "x", "VAPID_AUTO": "false",
           "DATABASE_URL": f"sqlite:///{tmp_path}/h.db"}
    env.pop("PYTHONPATH_FH", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert "LOADED webuntis ['timetable'] 15 True" in out.stdout, out.stdout + out.stderr[-1500:]


# --- compatibility guard --------------------------------------------------------------------------------------


def test_old_hub_is_inactive_and_explained(monkeypatch, untis):
    monkeypatch.setattr(module, "COMPATIBLE", False)
    plugin = WebUntisPlugin()
    assert plugin.is_configured() is False and plugin.active is False
    assert plugin.missing_settings() == ["needs Familienhub 1.0.0-beta.3 or newer"]
    check = run(plugin.check())
    assert check["status"] == "error" and "1.0.0-beta.3" in check["message"]
    assert run(plugin.timetable(MIA, FIRST, LAST, None)) == [] and run(plugin.timetable_people()) == []
    run(plugin.scheduled(None))
    result = run(plugin.test_settings({"WEBUNTIS_SERVER": SERVER}))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]
    assert untis.calls == []


def test_the_guard_detects_a_hub_without_the_hooks():
    import subprocess
    import sys
    code = """
import sys, types
import app.plugins as p
del p.Plugin.timetable            # what a pre-beta.3 hub looks like
sys.path.insert(0, %r)
from fh_plugin_webuntis import plugin
assert plugin.COMPATIBLE is False
print("GUARD OK", plugin.WebUntisPlugin().missing_settings())
""" % str(ROOT)
    env = {**__import__("os").environ, "DATA_DIR": str(ROOT / ".nonexistent-data"), "SECRET_KEY": "x", "VAPID_AUTO": "false",
           "DATABASE_URL": "sqlite://"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert "GUARD OK ['needs Familienhub 1.0.0-beta.3 or newer']" in out.stdout, out.stdout + out.stderr[-1500:]


def test_not_configured_is_inactive_and_quiet(monkeypatch, untis):
    monkeypatch.delenv("WEBUNTIS_PASSWORD")
    plugin = WebUntisPlugin()
    assert plugin.is_configured() is False and plugin.missing_settings() == ["WEBUNTIS_PASSWORD"]
    assert run(plugin.timetable(MIA, FIRST, LAST, None)) == [] and untis.calls == []


def test_a_malformed_server_setting_is_reported(monkeypatch):
    monkeypatch.setenv("WEBUNTIS_SERVER", "not a host")
    check = run(WebUntisPlugin().check())
    assert check["status"] == "error"
    assert run(WebUntisPlugin().timetable(MIA, FIRST, LAST, None)) == []


# --- timetable hook -------------------------------------------------------------------------------------------


def test_timetable_returns_the_childs_week(untis):
    week(untis)
    out = run(WebUntisPlugin().timetable(MIA, FIRST, LAST, None))
    assert [(x["date"], x["start"], x["title"], x["status"]) for x in out] == [
        ("2026-10-20", "08:00", "Maths", "normal"),
        ("2026-10-20", "08:50", "English", "cancelled"),
        ("2026-10-20", "09:55", "Art", "substitution")]
    assert out[1]["note"] == "Teacher ill" and out[1]["period"] == 2 and out[1]["location"] == "204"
    assert untis.calls[0] == "authenticate" and untis.calls[-1] == "logout" and untis.logouts == 1


def test_the_hub_accepts_what_the_plugin_returns(untis):
    from app.services.timetable_plugin import _lesson
    week(untis)
    for raw in run(WebUntisPlugin().timetable(MIA, FIRST, LAST, None)):
        assert _lesson(raw, FIRST, LAST) is not None


def test_answers_are_cached_per_child_and_range(untis):
    week(untis)
    plugin = WebUntisPlugin()
    first = run(plugin.timetable(MIA, FIRST, LAST, None))
    calls = len(untis.calls)
    assert run(plugin.timetable(MIA, FIRST, LAST, None)) == first and len(untis.calls) == calls
    run(plugin.timetable(SimpleNamespace(id=6, name="Ben", person_id="102"), FIRST, LAST, None))
    assert len(untis.calls) > calls


def test_hub_cannot_modify_the_cache(untis):
    week(untis)
    plugin = WebUntisPlugin()
    run(plugin.timetable(MIA, FIRST, LAST, None))[0]["title"] = "tampered"
    assert run(plugin.timetable(MIA, FIRST, LAST, None))[0]["title"] == "Maths"


def test_no_person_no_lessons(untis):
    assert run(WebUntisPlugin().timetable(SimpleNamespace(id=1, name="x", person_id=""), FIRST, LAST, None)) == []
    assert untis.calls == []


@pytest.mark.parametrize("answer", [httpx.Response(401), httpx.Response(500), httpx.Response(200, text="not json"),
                                    httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_a_broken_server_gives_empty_lessons(answer):
    import respx
    with respx.mock(using="httpx", assert_all_called=False) as router:
        router.post(f"{BASE}/jsonrpc.do").mock(side_effect=[answer] * 6)
        assert run(WebUntisPlugin().timetable(MIA, FIRST, LAST, None)) == []


def test_old_data_is_shown_when_the_server_fails(untis, monkeypatch):
    week(untis)
    plugin = WebUntisPlugin()
    good = run(plugin.timetable(MIA, FIRST, LAST, None))
    monkeypatch.setattr(module, "CACHE_TTL", 0.0)
    untis.router.post(f"{BASE}/jsonrpc.do").mock(return_value=httpx.Response(500))
    assert run(plugin.timetable(MIA, FIRST, LAST, None)) == good


def test_wrong_password_is_not_hammered(untis, monkeypatch):
    monkeypatch.setenv("WEBUNTIS_PASSWORD", "wrong")
    plugin = WebUntisPlugin()
    assert run(plugin.timetable(MIA, FIRST, LAST, None)) == []
    attempts = untis.calls.count("authenticate")
    assert attempts == 1
    other = SimpleNamespace(id=6, name="Ben", person_id="102")
    assert run(plugin.timetable(other, FIRST, LAST, None)) == [] and run(plugin.timetable_people()) == []
    assert untis.calls.count("authenticate") == attempts, "a rejected login blocks further tries for a while"


def test_slow_server_is_answered_in_time_and_the_fetch_finishes_in_the_background(untis, monkeypatch):
    week(untis)
    monkeypatch.setattr(module, "HUB_BUDGET", 0.05)

    async def slow(request):
        await asyncio.sleep(0.2)
        return untis.rpc(request)

    untis.router.post(f"{BASE}/jsonrpc.do").mock(side_effect=slow)

    async def go():
        plugin = WebUntisPlugin()
        first = await plugin.timetable(MIA, FIRST, LAST, None)
        await asyncio.gather(*plugin._inflight.values())
        return first, await plugin.timetable(MIA, FIRST, LAST, None)

    first, second = run(go())
    assert first == [] and len(second) == 3


def test_hub_cancelling_the_call_keeps_the_fetch_running(untis):
    week(untis)

    async def slow(request):
        await asyncio.sleep(0.2)
        return untis.rpc(request)

    untis.router.post(f"{BASE}/jsonrpc.do").mock(side_effect=slow)

    async def go():
        plugin = WebUntisPlugin()
        try:
            await asyncio.wait_for(plugin.timetable(MIA, FIRST, LAST, None), 0.05)  # what the hub does after 3 s
        except asyncio.TimeoutError:
            pass
        await asyncio.gather(*plugin._inflight.values())
        return await plugin.timetable(MIA, FIRST, LAST, None)

    assert len(run(go())) == 3 and untis.logouts == 1


# --- people ---------------------------------------------------------------------------------------------------


def test_people_of_a_parent_account(untis):
    people = run(WebUntisPlugin().timetable_people())
    assert people == [{"id": "101", "name": "Mia Muster"}, {"id": "102", "name": "Ben Muster"}]
    assert untis.logouts == 1


def test_people_when_everything_is_down():
    import respx
    with respx.mock(using="httpx", assert_all_called=False) as router:
        router.post(f"{BASE}/jsonrpc.do").mock(side_effect=httpx.ConnectError("down"))
        assert run(WebUntisPlugin().timetable_people()) == []


# --- connection test ------------------------------------------------------------------------------------------

VALUES = {"WEBUNTIS_SERVER": SERVER, "WEBUNTIS_SCHOOL": SCHOOL, "WEBUNTIS_USERNAME": USER, "WEBUNTIS_PASSWORD": PASSWORD}


def test_connection_test_ok(untis):
    result = run(WebUntisPlugin().test_settings(VALUES))
    assert result == {"ok": True, "level": "ok",
                      "message": f"Connected to {SCHOOL} as {USER}; students visible: 2"}
    assert untis.logouts == 1


def test_connection_test_uses_the_typed_values_not_the_saved_ones(untis, monkeypatch):
    monkeypatch.setenv("WEBUNTIS_PASSWORD", "saved-but-wrong")
    assert run(WebUntisPlugin().test_settings(VALUES))["ok"] is True
    assert run(WebUntisPlugin().test_settings({**VALUES, "WEBUNTIS_PASSWORD": "typed-wrong"}))["ok"] is False


def test_connection_test_student_account(untis):
    untis.person_type, untis.person_id = 5, 101
    assert run(WebUntisPlugin().test_settings(VALUES))["message"].endswith("students visible: 1")


def test_connection_test_account_without_students(untis):
    untis.students = []
    result = run(WebUntisPlugin().test_settings(VALUES))
    assert result["level"] == "warn" and result["ok"] is True and "parent or student" in result["message"]
    untis.person_type = 2
    assert "teacher" in run(WebUntisPlugin().test_settings(VALUES))["message"]


@pytest.mark.parametrize("change, words", [
    ({"WEBUNTIS_PASSWORD": "nope"}, "username or password"),
    ({"WEBUNTIS_SCHOOL": "other-school"}, "Server or school name not found"),
    ({"WEBUNTIS_SERVER": "not a host"}, "host name"),
    ({"WEBUNTIS_SERVER": ""}, "host name"),
    ({"WEBUNTIS_SCHOOL": ""}, "required"),
    ({"WEBUNTIS_USERNAME": " "}, "required"),
])
def test_connection_test_messages(untis, change, words):
    result = run(WebUntisPlugin().test_settings({**VALUES, **change}))
    assert result["ok"] is False and result["level"] == "error" and words in result["message"]


@pytest.mark.parametrize("answer, words", [
    (httpx.ConnectError("down"), "not reachable"),
    (httpx.ReadTimeout("slow"), "not reachable"),
    (httpx.Response(500), "not reachable"),
    (httpx.Response(200, text="<html>"), "unexpected"),
    (httpx.Response(302, headers={"location": "https://x.example.test/"}), "Server or school name not found"),
])
def test_connection_test_unreachable_or_odd(answer, words):
    import respx
    with respx.mock(using="httpx", assert_all_called=False) as router:
        router.post(f"{BASE}/jsonrpc.do").mock(side_effect=[answer] * 3)
        result = run(WebUntisPlugin().test_settings(VALUES))
    assert result["ok"] is False and words in result["message"]
    assert PASSWORD not in result["message"] and SERVER not in result["message"]


def test_secrets_never_reach_the_log(untis, caplog):
    caplog.set_level(logging.DEBUG)
    plugin = WebUntisPlugin()
    run(plugin.timetable(MIA, FIRST, LAST, None))
    run(plugin.test_settings({**VALUES, "WEBUNTIS_PASSWORD": "typed-wrong"}))
    untis.password = "changed"
    run(WebUntisPlugin().timetable(SimpleNamespace(id=9, name="x", person_id="102"), FIRST, LAST, None))
    # (httpx itself logs request URLs at INFO level; that is the hub's logging, and holds no credential)
    for secret in (PASSWORD, "typed-wrong", USER):
        assert secret not in caplog.text
