"""The scheduled job against a real (in-memory) hub database and the simulated WebUntis server."""

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app import models  # noqa: F401  (registers all tables)
from app.models.timetable import TimetableSource
from app.models.user import User

from fh_plugin_webuntis import plugin as module
from fh_plugin_webuntis.plugin import WebUntisPlugin

from .conftest import lesson

run = asyncio.run
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))  # Tuesday


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        def add(username, role, language=None, name=None):
            user = User(username=username, email=f"{username}@example.test", hashed_password="x", role=role,
                        language=language, display_name=name, is_active=True)
            session.add(user)
            session.flush()
            return user

        session.mum, session.dad = add("mum", "partner", "de"), add("dad", "admin", "en")
        session.mia, session.ben = add("mia", "child", "de", "Mia"), add("ben", "child", "en", "Ben")
        session.add(TimetableSource(child_id=session.mia.id, plugin_id="webuntis", person_id="101", person_name="Mia Muster"))
        session.add(TimetableSource(child_id=session.ben.id, plugin_id="webuntis", person_id="102", person_name="Ben Muster"))
        session.commit()
        yield session


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(plugin_id, **kwargs):
        calls.append({"plugin": plugin_id, **kwargs})
        return {"recipients": len(kwargs.get("user_ids", [])), "delivered": 1, "sent": True, "reason": None}

    monkeypatch.setattr(module, "notify", fake_notify)
    monkeypatch.setattr(module, "_now", lambda: NOW)
    return calls


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "plugins" / "webuntis" / "state.json"


@pytest.fixture(autouse=True)
def _path(monkeypatch, state_file):
    monkeypatch.setattr(module, "_state_path", lambda: state_file)


def seed(untis):
    untis.timetable[101] = [lesson(20261020, 800, 845)]
    untis.timetable[102] = [lesson(20261020, 800, 845)]


def cancel_tomorrow(untis):
    untis.timetable[101] += [lesson(20261021, 955, 1040, code="cancelled"), lesson(20261021, 1045, 1130, code="cancelled")]


def test_first_run_only_records_a_baseline(untis, db, sent, state_file):
    seed(untis)
    cancel_tomorrow(untis)
    run(WebUntisPlugin().scheduled(db))
    assert sent == []
    state = json.loads(state_file.read_text())
    assert len(state["children"]["%d" % db.mia.id]["days"]["2026-10-21"]) == 2
    assert untis.logouts == 1 and untis.logins == 1, "one session for all children"


def test_a_new_cancellation_is_pushed_once_to_parents_and_the_child(untis, db, sent):
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))                      # baseline
    cancel_tomorrow(untis)
    run(plugin.scheduled(db))
    by_user = {tuple(c["user_ids"]): c for c in sent}
    assert len(sent) == 3
    mum, dad, mia = by_user[(db.mum.id,)], by_user[(db.dad.id,)], by_user[(db.mia.id,)]
    assert mum["plugin"] == "webuntis" and mum["data"] == {"route": "/today"}
    assert "ausgefallen morgen" in mum["title"] and "cancelled tomorrow" in dad["title"] and "ausgefallen" in mia["title"]
    assert db.ben.id not in {i for c in sent for i in c["user_ids"]}, "Ben has no change"
    sent.clear()
    run(plugin.scheduled(db))                      # nothing new
    assert sent == []


def test_period_numbers_from_the_time_grid_appear_in_the_text(untis, db, sent):
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    cancel_tomorrow(untis)
    run(plugin.scheduled(db))
    titles = {c["title"] for c in sent}
    assert "Mia: 3rd–4th period Maths cancelled tomorrow" in titles
    assert "Mia: 3.–4. Stunde Maths ausgefallen morgen" in titles


def test_a_changed_substitution_is_reported_again(untis, db, sent):
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    untis.timetable[101].append(lesson(20261020, 850, 935, subject="Art", code="irregular", substText="Cover", room="R12"))
    run(plugin.scheduled(db))
    first = len(sent)
    assert first == 3 and any("Mia: 2nd period substitution (Art): Room R12, Smith, Cover today" == c["title"] for c in sent)
    untis.timetable[101][-1] = lesson(20261020, 850, 935, subject="Art", code="irregular", substText="Cover", room="R13")
    run(plugin.scheduled(db))
    assert len(sent) == 2 * first


def test_nothing_outside_6_to_22(untis, db, sent, monkeypatch):
    seed(untis)
    for hour in (5, 22, 23, 0):
        monkeypatch.setattr(module, "_now", lambda h=hour: NOW.replace(hour=h))
        run(WebUntisPlugin().scheduled(db))
    assert untis.calls == [] and sent == []
    monkeypatch.setattr(module, "_now", lambda: NOW.replace(hour=6))
    run(WebUntisPlugin().scheduled(db))
    assert untis.logins == 1


def test_notifications_can_be_switched_off(untis, db, sent, monkeypatch):
    monkeypatch.setenv("WEBUNTIS_NOTIFY", "false")
    seed(untis)
    run(WebUntisPlugin().scheduled(db))
    assert untis.calls == [] and sent == []


def test_notify_days_zero_is_today_only(untis, db, sent, monkeypatch):
    monkeypatch.setenv("WEBUNTIS_NOTIFY_DAYS", "0")
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    cancel_tomorrow(untis)
    run(plugin.scheduled(db))
    assert sent == []


def test_wider_window_sees_later_days(untis, db, sent, monkeypatch):
    monkeypatch.setenv("WEBUNTIS_NOTIFY_DAYS", "3")
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    untis.timetable[101].append(lesson(20261023, 800, 845, code="cancelled"))
    run(plugin.scheduled(db))
    assert any("Fri 23/10" in c["title"] for c in sent)


def test_undelivered_changes_are_retried(untis, db, sent, monkeypatch):
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    cancel_tomorrow(untis)

    async def limited(plugin_id, **kwargs):
        return {"recipients": 1, "delivered": 0, "sent": False, "reason": "rate_limited"}

    monkeypatch.setattr(module, "notify", limited)
    run(plugin.scheduled(db))
    monkeypatch.setattr(module, "notify", sent_notify(sent))
    run(plugin.scheduled(db))
    assert len(sent) == 3


def sent_notify(calls):
    async def fake(plugin_id, **kwargs):
        calls.append({"plugin": plugin_id, **kwargs})
        return {"recipients": 1, "delivered": 1, "sent": True, "reason": None}
    return fake


def test_nobody_to_notify_still_remembers(untis, db, sent, monkeypatch):
    async def nobody(plugin_id, **kwargs):
        return {"recipients": 0, "delivered": 0, "sent": False, "reason": "no_recipients"}

    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    cancel_tomorrow(untis)
    monkeypatch.setattr(module, "notify", nobody)
    run(plugin.scheduled(db))
    monkeypatch.setattr(module, "notify", sent_notify(sent))
    run(plugin.scheduled(db))
    assert sent == []


def test_server_down_changes_nothing(untis, db, sent, state_file):
    import httpx
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    before = state_file.read_text()
    untis.router.post("https://school.example.test/WebUntis/jsonrpc.do").mock(side_effect=httpx.ConnectError("down"))
    run(plugin.scheduled(db))
    assert sent == [] and state_file.read_text() == before


def test_one_child_failing_does_not_stop_the_others(untis, db, sent):
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    untis.timetable[101] = [lesson(20261020, 800, 845, subject="Art")]  # unchanged
    untis.timetable[102].append(lesson(20261020, 850, 935, code="cancelled"))
    run(plugin.scheduled(db))
    assert any(c["title"].startswith("Ben:") for c in sent)


def test_inactive_children_and_other_plugins_mappings_are_ignored(untis, db, sent):
    seed(untis)
    db.ben.is_active = False
    db.query(TimetableSource).filter_by(child_id=db.mia.id).update({"plugin_id": "other"})
    db.commit()
    run(WebUntisPlugin().scheduled(db))
    assert untis.calls == []


def test_wrong_password_stops_the_job_and_backs_off(untis, db, sent, monkeypatch):
    monkeypatch.setenv("WEBUNTIS_PASSWORD", "wrong")
    seed(untis)
    plugin = WebUntisPlugin()
    run(plugin.scheduled(db))
    run(plugin.scheduled(db))
    assert untis.calls.count("authenticate") == 1 and sent == []
